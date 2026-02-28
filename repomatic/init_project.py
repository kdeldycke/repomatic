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

"""Bundled data files, configuration templates, and repository initialization.

Provides a unified interface for accessing bundled data files from
``repomatic/data/`` and orchestrates repository bootstrapping via
``repomatic init``.

Available components (``repomatic init <component>``):

- ``workflows`` - Thin-caller workflow files
- ``labels`` - Label definitions (labels.toml + labeller rules)
- ``renovate`` - Renovate dependency update configuration (renovate.json5)
- ``changelog`` - Minimal changelog.md
- ``ruff`` - Merges ``[tool.ruff]`` into pyproject.toml
- ``pytest`` - Merges ``[tool.pytest]`` into pyproject.toml
- ``mypy`` - Merges ``[tool.mypy]`` into pyproject.toml
- ``bumpversion`` - Merges ``[tool.bumpversion]`` into pyproject.toml
- ``skills`` - Claude Code skill definitions (.claude/skills/)
"""

from __future__ import annotations

import glob as globmod
import logging
import re
import sys
from dataclasses import dataclass, field
from importlib.resources import as_file, files
from pathlib import Path, PurePosixPath
from urllib.request import urlretrieve

from . import __version__

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[import-not-found]

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Sequence

DEFAULT_REPO: str = "kdeldycke/repomatic"
"""Default upstream repository for reusable workflows."""


# ---------------------------------------------------------------------------
# Bundled data access
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InitConfig:
    """Metadata for configs that can be merged into pyproject.toml."""

    filename: str
    """Filename in repomatic/data/ directory."""

    tool_section: str
    """The [tool.X] section name to check for existence."""

    insert_after: Sequence[str]
    """Sections to insert after (in priority order)."""

    insert_before: Sequence[str]
    """Sections to insert before (if insert_after not found)."""

    description: str
    """Human-readable description for help text."""


# Registry of all exportable files: maps filename to default output path.
# None means stdout (for pyproject.toml templates that need merging).
EXPORTABLE_FILES: dict[str, str | None] = {
    # pyproject.toml config templates (no default path, output to stdout for merging).
    "mypy.toml": None,
    "ruff.toml": None,
    "pytest.toml": None,
    "bumpversion.toml": None,
    # Renovate configuration.
    "renovate.json5": "./renovate.json5",
    # Label configuration files.
    "labels.toml": "./labels.toml",
    "labeller-file-based.yaml": "./.github/labeller-file-based.yaml",
    "labeller-content-based.yaml": "./.github/labeller-content-based.yaml",
    # Linter configuration files.
    "zizmor.yaml": "./zizmor.yaml",
    # Claude Code skill definitions.
    "skill-repomatic-changelog.md": "./.claude/skills/repomatic-changelog/SKILL.md",
    "skill-repomatic-deps.md": "./.claude/skills/repomatic-deps/SKILL.md",
    "skill-repomatic-init.md": "./.claude/skills/repomatic-init/SKILL.md",
    "skill-repomatic-lint.md": "./.claude/skills/repomatic-lint/SKILL.md",
    "skill-repomatic-metadata.md": "./.claude/skills/repomatic-metadata/SKILL.md",
    "skill-repomatic-release.md": "./.claude/skills/repomatic-release/SKILL.md",
    "skill-repomatic-sync.md": "./.claude/skills/repomatic-sync/SKILL.md",
    "skill-repomatic-test.md": "./.claude/skills/repomatic-test/SKILL.md",
    "skill-repomatic-topics.md": "./.claude/skills/repomatic-topics/SKILL.md",
    # Workflow templates.
    "autofix.yaml": "./.github/workflows/autofix.yaml",
    "autolock.yaml": "./.github/workflows/autolock.yaml",
    "cancel-runs.yaml": "./.github/workflows/cancel-runs.yaml",
    "changelog.yaml": "./.github/workflows/changelog.yaml",
    "debug.yaml": "./.github/workflows/debug.yaml",
    "docs.yaml": "./.github/workflows/docs.yaml",
    "labels.yaml": "./.github/workflows/labels.yaml",
    "lint.yaml": "./.github/workflows/lint.yaml",
    "release.yaml": "./.github/workflows/release.yaml",
    "renovate.yaml": "./.github/workflows/renovate.yaml",
    "tests.yaml": "./.github/workflows/tests.yaml",
}

# Registry of configs that support `init` (merging into pyproject.toml).
# The insert_after/insert_before values follow pyproject-fmt's section ordering.
INIT_CONFIGS: dict[str, InitConfig] = {
    "ruff": InitConfig(
        filename="ruff.toml",
        tool_section="tool.ruff",
        insert_after=("tool.uv", "tool.uv.build-backend"),
        insert_before=("tool.pytest",),
        description="Ruff linter/formatter configuration",
    ),
    "pytest": InitConfig(
        filename="pytest.toml",
        tool_section="tool.pytest",
        insert_after=("tool.ruff", "tool.ruff.format"),
        insert_before=("tool.mypy",),
        description="Pytest test configuration",
    ),
    "mypy": InitConfig(
        filename="mypy.toml",
        tool_section="tool.mypy",
        insert_after=("tool.pytest",),
        insert_before=("tool.nuitka", "tool.bumpversion"),
        description="Mypy type checking configuration",
    ),
    "bumpversion": InitConfig(
        filename="bumpversion.toml",
        tool_section="tool.bumpversion",
        insert_after=("tool.nuitka", "tool.mypy"),
        insert_before=("tool.typos",),
        description="bump-my-version configuration",
    ),
}


def get_data_content(filename: str) -> str:
    """Get the content of a bundled data file.

    This is the low-level function for reading any file from ``repomatic/data/``.

    :param filename: Name of the file to retrieve (e.g., "labels.toml").
    :return: Content of the file as a string.
    :raises FileNotFoundError: If the file doesn't exist.
    """
    data_files = files("repomatic.data")
    with as_file(data_files.joinpath(filename)) as path:
        if path.exists():
            return path.read_text(encoding="UTF-8")

    msg = f"Data file not found: {filename}"
    raise FileNotFoundError(msg)


def _get_renovate_config() -> str:
    """Get Renovate config, with repo-specific settings stripped.

    When running from the source repository, reads the root ``renovate.json5``
    and removes repo-specific settings (``customManagers``, ``assignees``).

    When installed as a package, falls back to the pre-processed bundled file
    in ``repomatic/data/renovate.json5``.

    :return: The clean Renovate configuration content.
    """
    root_path = Path(__file__).parent.parent / "renovate.json5"

    # When installed as a package, the root file won't exist.
    # Fall back to the bundled pre-processed version.
    if not root_path.exists():
        return get_data_content("renovate.json5")

    content = root_path.read_text(encoding="UTF-8")

    # Remove assignees line.
    content = re.sub(r"\s*assignees:\s*\[[^\]]*\],?\n", "\n", content)

    # Remove customManagers section and its preceding comment.
    # Find where customManagers starts (including its comment).
    cm_match = re.search(
        r"\n\s*//[^\n]*[Cc]ustom [Mm]anagers[^\n]*\n\s*customManagers:", content
    )
    if cm_match:
        # Find the closing of vulnerabilityAlerts (the section before customManagers).
        # Keep everything up to and including that closing brace and comma.
        va_end = re.search(r"(vulnerabilityAlerts:\s*\{[^}]*\},?\s*)\n", content)
        if va_end:
            # Keep content up to end of vulnerabilityAlerts, then close the object.
            content = content[: va_end.end()].rstrip().rstrip(",") + "\n}\n"

    return content


def export_content(filename: str) -> str:
    """Get the content of any exportable bundled file.

    :param filename: The filename (e.g., "ruff.toml", "labels.toml", "release.yaml").
    :return: Content of the file as a string.
    :raises ValueError: If the file is not in the registry.
    :raises FileNotFoundError: If the file doesn't exist.
    """
    if filename not in EXPORTABLE_FILES:
        supported = ", ".join(EXPORTABLE_FILES.keys())
        msg = f"Unknown file: {filename!r}. Supported: {supported}"
        raise ValueError(msg)

    # Special handling for renovate.json5: read from root and strip
    # repo-specific settings.
    if filename == "renovate.json5":
        return _get_renovate_config()

    return get_data_content(filename)


# ---------------------------------------------------------------------------
# pyproject.toml config merging
# ---------------------------------------------------------------------------


def _to_pyproject_format(template: str, tool_section: str) -> str:
    """Transform native config format to pyproject.toml format.

    Adds the [tool.X] prefix to all sections in the template.

    :param template: The native format template content.
    :param tool_section: The tool section name (e.g., "tool.ruff").
    :return: The transformed content with [tool.X] prefixes.
    """
    lines = template.splitlines()
    result = []
    has_root_section = False

    for line in lines:
        # Match section headers: [section] or [[section]].
        section_match = re.match(r"^(\[+)([^\]]+)(\]+)\s*$", line)
        if section_match:
            brackets_open = section_match.group(1)
            section_name = section_match.group(2)
            brackets_close = section_match.group(3)

            # Transform section name to include tool prefix.
            new_section = f"{tool_section}.{section_name}"
            result.append(f"{brackets_open}{new_section}{brackets_close}")
        else:
            # Non-section line: check if we need to add root section first.
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and not has_root_section:
                # First non-comment, non-empty line - add root section.
                result.append(f"[{tool_section}]")
                has_root_section = True
            result.append(line)

    return "\n".join(result)


def _extract_dev_config_block() -> str:
    """Extract the dev versioning keys from the bumpversion template.

    Reads ``bumpversion.toml`` and returns only the root-level keys needed for
    dev versioning (``ignore_missing_files``, ``parse``, ``serialize``) plus the
    ``[parts.dev]`` section, formatted for ``[tool.bumpversion]`` in
    pyproject.toml.

    :return: The dev config block as text, ready for insertion.
    """
    native_template = export_content("bumpversion.toml")
    lines = native_template.splitlines()

    # Collect root-level dev keys and the [parts.dev] section.
    dev_keys = {"ignore_missing_files", "parse", "serialize"}
    result_lines: list[str] = []
    in_dev_key = False
    in_parts_dev = False

    for line in lines:
        stripped = line.strip()

        # Detect [parts.dev] section start.
        if re.match(r"^\[parts\.dev\]", stripped):
            in_parts_dev = True
            result_lines.append(f"[tool.bumpversion.{stripped[1:-1]}]")
            continue

        # Detect end of [parts.dev] section (next section or [[files]]).
        if in_parts_dev and re.match(r"^\[", stripped):
            in_parts_dev = False

        if in_parts_dev:
            result_lines.append(line)
            continue

        # Detect root-level dev keys.
        key_match = re.match(r"^(\w+)\s*=", stripped)
        if key_match and key_match.group(1) in dev_keys:
            in_dev_key = True

        if in_dev_key:
            result_lines.append(line)
            # Multi-line values end when we see a line with `]`.
            if stripped.endswith("]") or (
                "=" in stripped and not stripped.endswith("[")
            ):
                in_dev_key = False
            continue

        # Collect comments that precede dev keys.
        if stripped.startswith("#") and not result_lines:
            # Skip file-level comments at the top.
            continue
        if stripped.startswith("#"):
            # Check if the next non-comment line is a dev key.
            result_lines.append(line)
            continue

    # Clean up: remove trailing comment-only lines that don't belong.
    # Walk backwards and drop comment lines that aren't followed by a dev key.
    cleaned: list[str] = []
    pending_comments: list[str] = []
    for line in result_lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            pending_comments.append(line)
        else:
            if stripped:
                cleaned.extend(pending_comments)
            pending_comments = []
            cleaned.append(line)

    return "\n".join(cleaned)


def _update_bumpversion_config(
    content: str,
    pyproject_path: Path,
) -> str | None:
    """Update an existing ``[tool.bumpversion]`` config with dev versioning keys.

    Detects whether the existing config already has dev versioning support
    (``parse`` key) and, if not, injects the required keys. Also appends
    ``.dev0`` to ``current_version`` if it lacks a dev suffix.

    After updating pyproject.toml content in memory, applies version changes to
    all files managed by the ``[[tool.bumpversion.files]]`` entries.

    :param content: The current pyproject.toml content.
    :param pyproject_path: Path to pyproject.toml (for resolving managed files).
    :return: Modified pyproject.toml content, or ``None`` if already up to date.
    """
    parsed = tomllib.loads(content)
    bv_config = parsed.get("tool", {}).get("bumpversion", {})

    # Already has dev versioning configured.
    if "parse" in bv_config:
        logging.info("[tool.bumpversion] already has dev versioning configured.")
        return None

    current_version = bv_config.get("current_version", "")

    # Determine the new version with .dev0 suffix.
    needs_dev_suffix = not re.search(r"\.dev\d+$", current_version)
    new_version = f"{current_version}.dev0" if needs_dev_suffix else current_version

    # Extract the dev config block from the template.
    dev_block = _extract_dev_config_block()

    # Find the insertion point: after the last root-level key in
    # [tool.bumpversion], before [[tool.bumpversion.files]] or
    # [tool.bumpversion.parts.*] or the next top-level section.
    bv_section_match = re.search(r"^\[tool\.bumpversion\]\s*$", content, re.MULTILINE)
    if not bv_section_match:
        return None

    # Find the end of root-level keys in [tool.bumpversion].
    # Look for the first subsection, array section, or next top-level section.
    after_header = bv_section_match.end()
    next_section_pattern = re.compile(
        r"^(?:\[\[tool\.bumpversion\."
        r"|\[tool\.bumpversion\."
        r"|\[[a-z])",
        re.MULTILINE,
    )
    next_section = next_section_pattern.search(content, after_header)

    if next_section:
        insertion_point = next_section.start()
    else:
        insertion_point = len(content)

    # Insert the dev config block.
    before = content[:insertion_point].rstrip()
    after = content[insertion_point:].lstrip()

    modified = before + "\n" + dev_block.strip() + "\n"
    if after:
        modified += "\n" + after

    # Update current_version if it needs .dev0 suffix.
    if needs_dev_suffix and current_version:
        modified = _update_current_version(modified, current_version, new_version)

        # Apply version changes to managed files.
        # pyproject.toml is updated in-memory since the caller writes it.
        files_entries = bv_config.get("files", [])
        modified = _update_managed_files(
            pyproject_path,
            files_entries,
            current_version,
            new_version,
            modified,
        )

    return modified


def _update_current_version(content: str, old_version: str, new_version: str) -> str:
    """Replace ``current_version`` in the bumpversion section.

    Only replaces the first occurrence within ``[tool.bumpversion]`` to avoid
    touching version strings in other sections.

    :param content: The pyproject.toml content.
    :param old_version: The current version string (e.g., ``"7.5.3"``).
    :param new_version: The new version string (e.g., ``"7.5.3.dev0"``).
    :return: The modified content.
    """
    # Match current_version = "X.Y.Z" in the bumpversion section.
    pattern = r'(current_version\s*=\s*")' + re.escape(old_version) + r'"'
    return re.sub(pattern, rf'\g<1>{new_version}"', content, count=1)


def _update_managed_files(
    pyproject_path: Path,
    files_entries: list[dict[str, str]],
    old_version: str,
    new_version: str,
    pyproject_content: str,
) -> str:
    """Apply version updates to files managed by bumpversion.

    Reads each file entry from ``[[tool.bumpversion.files]]``, interpolates
    ``{current_version}`` and ``{new_version}`` in the search/replace patterns,
    and applies the substitution.

    Updates to ``pyproject.toml`` are applied to ``pyproject_content`` in memory
    (since the caller writes the final content). All other files are written to
    disk directly.

    :param pyproject_path: Path to pyproject.toml.
    :param files_entries: List of file entry dicts from bumpversion config.
    :param old_version: The old version string.
    :param new_version: The new version string.
    :param pyproject_content: The in-memory pyproject.toml content.
    :return: The updated pyproject.toml content.
    """
    project_dir = pyproject_path.parent

    for entry in files_entries:
        search = entry.get("search", "{current_version}")
        replace = entry.get("replace", "{new_version}")

        # Interpolate version placeholders.
        search_str = search.replace("{current_version}", old_version)
        replace_str = replace.replace("{new_version}", new_version)

        # Resolve file paths.
        glob_pattern = entry.get("glob")
        filename = entry.get("filename")

        if glob_pattern:
            # Expand glob relative to project directory.
            paths = [
                Path(p)
                for p in globmod.glob(str(project_dir / glob_pattern), recursive=True)
            ]
        elif filename:
            paths = [project_dir / filename]
        else:
            continue

        for path in paths:
            # pyproject.toml is updated in memory, not on disk.
            if path.resolve() == pyproject_path.resolve():
                if search_str in pyproject_content:
                    pyproject_content = pyproject_content.replace(
                        search_str, replace_str
                    )
                    logging.info(
                        f"Updated version in {path} (in memory): "
                        f"{old_version!r} -> {new_version!r}"
                    )
                continue

            if not path.exists():
                logging.debug(f"Skipping missing file: {path}")
                continue
            file_content = path.read_text(encoding="UTF-8")
            if search_str in file_content:
                updated = file_content.replace(search_str, replace_str)
                path.write_text(updated, encoding="UTF-8")
                logging.info(
                    f"Updated version in {path}: {old_version!r} -> {new_version!r}"
                )

    return pyproject_content


def init_config(config_type: str, pyproject_path: Path | None = None) -> str | None:
    """Initialize a configuration by merging it into pyproject.toml.

    Reads the pyproject.toml file, checks if the tool section already exists,
    and if not, inserts the bundled template at the appropriate location.

    The template is stored in native format (without [tool.X] prefix) and is
    transformed to pyproject.toml format during insertion.

    This function works with the file as text to preserve comments and formatting.

    :param config_type: The configuration type (e.g., "ruff", "bumpversion").
    :param pyproject_path: Path to pyproject.toml. Defaults to ``./pyproject.toml``.
    :return: The modified pyproject.toml content, or ``None`` if no changes needed.
    :raises ValueError: If the config type is not supported.
    """
    if config_type not in INIT_CONFIGS:
        supported = ", ".join(INIT_CONFIGS.keys())
        msg = f"Unknown config type: {config_type!r}. Supported: {supported}"
        raise ValueError(msg)

    config = INIT_CONFIGS[config_type]

    if pyproject_path is None:
        pyproject_path = Path("pyproject.toml")

    if not pyproject_path.exists():
        logging.error(f"File not found: {pyproject_path}")
        return None

    content = pyproject_path.read_text(encoding="UTF-8")

    # Check if the config section already exists.
    section_pattern = rf"^\[{re.escape(config.tool_section)}\]"
    if re.search(section_pattern, content, re.MULTILINE):
        # For bumpversion, try updating existing config with dev versioning.
        if config_type == "bumpversion":
            return _update_bumpversion_config(content, pyproject_path)
        logging.info(f"[{config.tool_section}] already exists in {pyproject_path.name}")
        return None

    # Get the template content and transform to pyproject.toml format.
    native_template = export_content(config.filename)
    template = _to_pyproject_format(native_template, config.tool_section)

    # Find insertion point using the config's preferences.
    insertion_index = _find_insertion_point(content, config)

    # Ensure proper spacing.
    before = content[:insertion_index].rstrip()
    after = content[insertion_index:].lstrip()

    # Build the new content with proper newlines.
    new_content = before + "\n\n" + template.strip() + "\n"
    if after:
        new_content += "\n" + after

    return new_content


def _find_insertion_point(content: str, config: InitConfig) -> int:
    """Find the best insertion point for a config section.

    :param content: The pyproject.toml content.
    :param config: The configuration type metadata.
    :return: The character index where the new section should be inserted.
    """
    # Pattern to match tool sections: [tool.xxx] or [[tool.xxx.yyy]].
    tool_section_pattern = re.compile(r"^\[+tool\.[^\]]+\]+", re.MULTILINE)

    # Find all tool sections and their positions.
    sections = list(tool_section_pattern.finditer(content))

    # Strategy 1: Find insert_after sections and insert after the last one.
    for target in config.insert_after:
        for i, match in enumerate(sections):
            section_name = match.group().strip("[]")
            if section_name == target or section_name.startswith(f"{target}."):
                # Find the next section that isn't a subsection of this one.
                for j in range(i + 1, len(sections)):
                    next_section = sections[j].group().strip("[]")
                    if not next_section.startswith(f"{target}."):
                        return sections[j].start()
                # No more sections, append at end.
                return len(content)

    # Strategy 2: Find insert_before sections and insert before the first one.
    for target in config.insert_before:
        for match in sections:
            section_name = match.group().strip("[]")
            if section_name == target or section_name.startswith(f"{target}."):
                return match.start()

    # Strategy 3: Append at the end.
    return len(content)


# ---------------------------------------------------------------------------
# Repository initialization
# ---------------------------------------------------------------------------


def default_version_pin() -> str:
    """Derive the default version pin from ``__version__``.

    Strips any ``.dev0`` suffix and prefixes with ``v``. For example,
    ``"5.10.0.dev0"`` becomes ``"v5.10.0"``.
    """
    version = re.sub(r"\.dev\d*$", "", __version__)
    return f"v{version}"


# Components that generate files (not tool config merges).
FILE_COMPONENTS: dict[str, str] = {
    "changelog": "Minimal changelog.md",
    "labels": "Label config files (labels.toml + labeller rules)",
    "linters": "Linter config files (zizmor.yaml)",
    "renovate": "Renovate config (renovate.json5)",
    "skills": "Claude Code skill definitions (.claude/skills/)",
    "workflows": "Thin-caller workflow files",
}
"""Components that create files during init, with descriptions."""

# Tool config components that merge into pyproject.toml.
TOOL_COMPONENTS: dict[str, str] = {k: v.description for k, v in INIT_CONFIGS.items()}
"""Tool config components merged into pyproject.toml during init."""

ALL_COMPONENTS: dict[str, str] = {**FILE_COMPONENTS, **TOOL_COMPONENTS}
"""All available init components."""

DEFAULT_COMPONENTS: tuple[str, ...] = tuple(sorted(FILE_COMPONENTS.keys()))
"""Components included when no explicit selection is made."""

# Maps component names to (source filename, relative output path) tuples.
COMPONENT_FILES: dict[str, tuple[tuple[str, str], ...]] = {
    "labels": (
        ("labeller-content-based.yaml", ".github/labeller-content-based.yaml"),
        ("labeller-file-based.yaml", ".github/labeller-file-based.yaml"),
        ("labels.toml", "labels.toml"),
    ),
    "linters": (("zizmor.yaml", "zizmor.yaml"),),
    "renovate": (("renovate.json5", "renovate.json5"),),
    "skills": (
        ("skill-repomatic-changelog.md", ".claude/skills/repomatic-changelog/SKILL.md"),
        ("skill-repomatic-deps.md", ".claude/skills/repomatic-deps/SKILL.md"),
        ("skill-repomatic-init.md", ".claude/skills/repomatic-init/SKILL.md"),
        ("skill-repomatic-lint.md", ".claude/skills/repomatic-lint/SKILL.md"),
        ("skill-repomatic-metadata.md", ".claude/skills/repomatic-metadata/SKILL.md"),
        ("skill-repomatic-release.md", ".claude/skills/repomatic-release/SKILL.md"),
        ("skill-repomatic-sync.md", ".claude/skills/repomatic-sync/SKILL.md"),
        ("skill-repomatic-test.md", ".claude/skills/repomatic-test/SKILL.md"),
        ("skill-repomatic-topics.md", ".claude/skills/repomatic-topics/SKILL.md"),
    ),
}
"""Bundled config files per component, with their output paths."""


@dataclass
class InitResult:
    """Result of a repository initialization run."""

    created: list[str] = field(default_factory=list)
    """Relative paths of created files."""

    skipped: list[str] = field(default_factory=list)
    """Relative paths of skipped (already existing) files."""

    excluded_components: list[str] = field(default_factory=list)
    """Component names excluded by ``init-exclude`` config."""

    excluded_workflows: list[str] = field(default_factory=list)
    """Workflow filenames excluded by ``workflow-sync-exclude`` config."""

    warnings: list[str] = field(default_factory=list)
    """Warning messages emitted during initialization."""


def run_init(
    output_dir: Path,
    components: Sequence[str] = (),
    version: str | None = None,
    repo: str = DEFAULT_REPO,
    overwrite: bool = False,
) -> InitResult:
    """Bootstrap a repository for use with ``kdeldycke/repomatic``.

    Creates thin-caller workflow files, exports configuration files, and
    generates a minimal ``changelog.md`` if missing. By default, existing
    files are skipped; use ``overwrite=True`` to replace them.

    :param output_dir: Root directory of the target repository.
    :param components: Components to initialize. Empty means all defaults.
    :param version: Version pin for upstream workflows (e.g., ``v5.10.0``).
    :param repo: Upstream repository containing reusable workflows.
    :param overwrite: Overwrite existing files instead of skipping.
    :return: Summary of created, skipped, and warned items.
    """
    if version is None:
        version = default_version_pin()

    selected = set(components) if components else set(DEFAULT_COMPONENTS)
    result = InitResult()

    # Apply config exclusions when no explicit components given.
    workflow_exclude: frozenset[str] = frozenset()
    if not components:
        from .metadata import load_repomatic_config

        config = load_repomatic_config()
        init_exclude: list[str] = config.get(
            "init-exclude",
            ["labels", "linters", "skills"],
        )
        if init_exclude:
            exclude_set = set(init_exclude)
            unknown = exclude_set - set(ALL_COMPONENTS)
            for name in sorted(unknown):
                logging.warning(
                    f"Unknown component in init-exclude: {name!r}. "
                    f"Known components: {', '.join(sorted(ALL_COMPONENTS))}"
                )
            actually_excluded = exclude_set & selected
            selected -= exclude_set
            result.excluded_components = sorted(actually_excluded)

        wf_exclude: list[str] = config.get("workflow-sync-exclude", [])
        if wf_exclude:
            workflow_exclude = frozenset(wf_exclude)

    # Workflows.
    if "workflows" in selected:
        _init_workflows(
            output_dir, repo, version, overwrite, result, exclude=workflow_exclude
        )

    # Config file components (labels, linters, renovate, skills).
    for component_name in ("labels", "linters", "renovate", "skills"):
        if component_name in selected:
            _init_config_files(output_dir, component_name, overwrite, result)

    # Fetch extra label files from [tool.repomatic] config.
    if "labels" in selected:
        _fetch_extra_labels(output_dir, result)

    # Changelog.
    if "changelog" in selected:
        _init_changelog(output_dir, overwrite, result)

    # Tool configs (merged into pyproject.toml).
    tool_configs = selected & set(INIT_CONFIGS.keys())
    if tool_configs:
        _init_tool_configs(output_dir, sorted(tool_configs), result)

    return result


def _init_workflows(
    output_dir: Path,
    repo: str,
    version: str,
    overwrite: bool,
    result: InitResult,
    *,
    exclude: frozenset[str] = frozenset(),
) -> None:
    """Generate thin-caller workflow files."""
    # Lazy import to avoid circular dependency with workflow_sync.
    from .github.workflow_sync import REUSABLE_WORKFLOWS, generate_thin_caller

    workflows = REUSABLE_WORKFLOWS
    if exclude:
        all_known = set(REUSABLE_WORKFLOWS)
        unknown = exclude - all_known
        for name in sorted(unknown):
            logging.warning(
                f"Unknown workflow in workflow-sync-exclude: {name!r}. "
                f"Known workflows: {', '.join(sorted(all_known))}"
            )
        actually_excluded = exclude & all_known
        workflows = tuple(w for w in workflows if w not in exclude)
        result.excluded_workflows = sorted(actually_excluded)

    workflows_dir = output_dir / ".github" / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    for filename in workflows:
        target = workflows_dir / filename
        rel = target.relative_to(output_dir).as_posix()
        if target.exists() and not overwrite:
            result.skipped.append(rel)
            logging.debug(f"Skipped existing: {rel}")
            continue
        content = generate_thin_caller(filename, repo, version)
        target.write_text(content, encoding="UTF-8")
        result.created.append(rel)
        logging.info(f"Created: {rel}")


def _init_config_files(
    output_dir: Path,
    component_name: str,
    overwrite: bool,
    result: InitResult,
) -> None:
    """Export bundled config files for a component."""
    for source_name, rel_path in COMPONENT_FILES[component_name]:
        target = output_dir / rel_path
        rel = target.relative_to(output_dir).as_posix()
        if target.exists() and not overwrite:
            result.skipped.append(rel)
            logging.debug(f"Skipped existing: {rel}")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        content = export_content(source_name)
        target.write_text(content, encoding="UTF-8")
        result.created.append(rel)
        logging.info(f"Created: {rel}")


def _init_changelog(
    output_dir: Path,
    overwrite: bool,
    result: InitResult,
) -> None:
    """Create a minimal changelog.md."""
    changelog_path = output_dir / "changelog.md"
    rel = changelog_path.relative_to(output_dir).as_posix()
    if changelog_path.exists() and not overwrite:
        result.skipped.append(rel)
        logging.debug(f"Skipped existing: {rel}")
        return
    changelog_content = (
        "# Changelog\n"
        "\n"
        "## [Unreleased](https://github.com/USER/REPO/compare/main...main)\n"
    )
    changelog_path.write_text(changelog_content, encoding="UTF-8")
    result.created.append(rel)
    logging.info(f"Created: {rel}")


def _fetch_extra_labels(
    output_dir: Path,
    result: InitResult,
) -> None:
    """Download extra label files from ``[tool.repomatic]`` config.

    Reads ``extra-label-files`` URLs and downloads each file to an
    ``extra-labels/`` subdirectory under ``output_dir``.
    Does nothing if no URLs are configured.
    """
    # Lazy import to avoid circular dependency.
    from .metadata import load_repomatic_config

    config = load_repomatic_config()
    urls = config.get("extra-label-files", [])
    if not urls:
        logging.debug("No extra-label-files configured.")
        return

    target_dir = output_dir / "extra-labels"
    target_dir.mkdir(exist_ok=True)
    for url in urls:
        url = url.strip()
        if not url:
            continue
        filename = PurePosixPath(url).name
        target = target_dir / filename
        rel = target.relative_to(output_dir).as_posix()
        logging.info(f"Downloading {url} -> {target}")
        urlretrieve(url, target)  # noqa: S310
        result.created.append(rel)


def _init_tool_configs(
    output_dir: Path,
    tool_configs: Sequence[str],
    result: InitResult,
) -> None:
    """Merge selected tool configs into pyproject.toml."""
    pyproject_path = output_dir / "pyproject.toml"
    if not pyproject_path.exists():
        result.warnings.append(
            "pyproject.toml not found; skipping tool config initialization."
        )
        logging.warning(result.warnings[-1])
        return
    for config_type in tool_configs:
        section = INIT_CONFIGS[config_type].tool_section
        had_section = re.search(
            rf"^\[{re.escape(section)}\]",
            pyproject_path.read_text(encoding="UTF-8"),
            re.MULTILINE,
        )
        merged = init_config(config_type, pyproject_path)
        if merged is None:
            logging.info(f"[{section}] already up to date, skipped.")
        else:
            pyproject_path.write_text(merged, encoding="UTF-8")
            if had_section:
                logging.info(f"Updated [{section}].")
            else:
                logging.info(f"Merged [{section}].")
