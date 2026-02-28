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

"""Generation, sync, and lint for downstream workflows.

Downstream repositories consuming reusable workflows from ``kdeldycke/repomatic``
manually write caller workflows that often miss triggers like
``workflow_dispatch``. This module provides tools to generate, synchronize, and
lint those callers by parsing the canonical workflow definitions.

See :class:`WorkflowFormat` for available output formats and their behavior.

.. caution::
    PyYAML destroys formatting and comments on round-trip. Until we find a
    layout-preserving YAML parsing and rendering solution, we use raw text
    extraction to manipulate workflow files while preserving formatting and
    comments.
"""

from __future__ import annotations

import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .. import __version__
from ..init_project import export_content, get_data_content
from .actions import AnnotationLevel, emit_annotation

if sys.version_info >= (3, 11):
    from enum import StrEnum
else:
    from backports.strenum import StrEnum  # type: ignore[import-not-found]

TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Any, Final


class WorkflowFormat(StrEnum):
    """Output format for generated workflow files."""

    FULL_COPY = "full-copy"
    """Verbatim copy of the canonical workflow file.

    Creates or overwrites the target with the full upstream content. Useful for
    workflows that need no downstream customization.
    """

    HEADER_ONLY = "header-only"
    """Sync only the header (``name``, ``on``, ``concurrency``) from upstream.

    Replaces everything before the ``jobs:`` line in an existing downstream file
    with the canonical header. The downstream ``jobs:`` section is preserved.
    Requires the target file to already exist; does not create new files.
    """

    SYMLINK = "symlink"
    """Create a symbolic link to the canonical workflow file.

    Creates or overwrites the target as a symlink pointing to the upstream
    workflow in the bundled data directory.
    """

    THIN_CALLER = "thin-caller"
    """Generate a minimal caller that delegates to the reusable upstream workflow.

    Creates or overwrites the target with a lightweight workflow containing only
    ``name``, ``on`` triggers, and a ``jobs:`` section that calls the upstream
    workflow via ``workflow_call``. Only works for reusable workflows (those with
    a ``workflow_call`` trigger).
    """


DEFAULT_REPO: Final[str] = "kdeldycke/repomatic"
"""Default upstream repository for reusable workflows."""

DEFAULT_VERSION: Final[str] = "main" if ".dev" in __version__ else f"v{__version__}"
"""Default version reference for upstream workflows.

For release builds (e.g., ``repomatic==5.11.0``), this resolves to the
corresponding tag (``v5.11.0``). For development builds (``5.11.1.dev0``),
it falls back to ``main`` since the tag does not exist yet.
"""

NON_REUSABLE_WORKFLOWS: Final[frozenset[str]] = frozenset(("tests.yaml",))
"""Workflows without ``workflow_call`` that cannot be used as thin callers."""

REUSABLE_WORKFLOWS: Final[tuple[str, ...]] = (
    "autofix.yaml",
    "autolock.yaml",
    "cancel-runs.yaml",
    "changelog.yaml",
    "debug.yaml",
    "docs.yaml",
    "labels.yaml",
    "lint.yaml",
    "release.yaml",
    "renovate.yaml",
)
"""Workflow filenames that support ``workflow_call`` triggers."""

ALL_WORKFLOW_FILES: Final[tuple[str, ...]] = tuple(
    sorted(set(REUSABLE_WORKFLOWS) | NON_REUSABLE_WORKFLOWS)
)
"""All workflow filenames (reusable and non-reusable)."""


def _extract_raw_section(content: str, section_name: str) -> str | None:
    """Extract a top-level YAML section as raw text.

    Finds a line matching ``{section_name}:`` at column 0 and returns it along
    with all indented continuation lines (including comments). Returns ``None``
    if the section is not found.

    :param content: Full workflow file content.
    :param section_name: Top-level key to extract (e.g., ``"concurrency"``).
    :return: Raw text of the section, or ``None`` if absent.
    """
    pattern = re.compile(rf"^{re.escape(section_name)}:", re.MULTILINE)
    match = pattern.search(content)
    if match is None:
        return None

    lines = content[match.start() :].split("\n")
    result = [lines[0]]
    for line in lines[1:]:
        # Stop at the next top-level key (non-empty, non-comment, no indent).
        if line and not line[0].isspace() and not line.startswith("#"):
            break
        result.append(line)

    # Strip trailing blank lines.
    while result and not result[-1].strip():
        result.pop()

    return "\n".join(result)


def _extract_raw_header(content: str) -> str:
    """Extract everything before the ``jobs:`` line as raw text.

    :param content: Full workflow file content.
    :return: Raw header text (up to but not including ``jobs:``).
    :raises ValueError: If no ``jobs:`` line is found.
    """
    match = re.search(r"^jobs:", content, re.MULTILINE)
    if match is None:
        msg = "No 'jobs:' line found in workflow content."
        raise ValueError(msg)
    return content[: match.start()]


@dataclass(frozen=True)
class WorkflowTriggerInfo:
    """Parsed trigger information from a canonical workflow."""

    name: str
    """Workflow display name from the ``name:`` field."""

    filename: str
    """Workflow filename (e.g., ``release.yaml``)."""

    non_call_triggers: dict[str, Any]
    """All triggers except ``workflow_call``, preserving their configuration."""

    call_inputs: dict[str, Any]
    """Inputs defined under ``workflow_call.inputs``."""

    call_secrets: dict[str, Any]
    """Secrets defined under ``workflow_call.secrets``."""

    has_workflow_call: bool
    """Whether the workflow defines a ``workflow_call`` trigger."""

    concurrency: dict[str, Any] | None
    """Parsed concurrency configuration, or ``None`` if absent."""

    raw_concurrency: str | None
    """Raw text of the concurrency block, preserving formatting and comments."""


@dataclass
class LintResult:
    """Result of a single lint check."""

    message: str
    """Human-readable description of the finding."""

    is_issue: bool
    """Whether this result represents a problem."""

    level: AnnotationLevel = field(default=AnnotationLevel.WARNING)
    """Severity level for GitHub Actions annotations."""


def extract_trigger_info(filename: str) -> WorkflowTriggerInfo:
    """Extract trigger information from a bundled canonical workflow.

    Parses the workflow YAML and separates ``workflow_call`` configuration from
    other triggers.

    :param filename: Workflow filename (e.g., ``release.yaml``).
    :return: Parsed trigger information.
    :raises FileNotFoundError: If the workflow file is not bundled.
    """
    content = get_data_content(filename)
    data = yaml.safe_load(content)

    name = data.get("name", filename)

    # Handle YAML parsing of bare ``on`` keyword: PyYAML reads bare ``on`` as
    # boolean ``True``, while quoted ``"on"`` is a string key.
    triggers: dict[str, Any] = {}
    if True in data:
        triggers = data[True] or {}
    elif "on" in data:
        triggers = data["on"] or {}

    has_workflow_call = "workflow_call" in triggers
    call_config = triggers.get("workflow_call") or {}
    call_inputs = call_config.get("inputs") or {}
    call_secrets = call_config.get("secrets") or {}

    # Collect all non-workflow_call triggers.
    non_call_triggers: dict[str, Any] = {
        trigger_name: trigger_config
        for trigger_name, trigger_config in triggers.items()
        if trigger_name != "workflow_call"
    }

    # Extract concurrency block (parsed and raw).
    concurrency = data.get("concurrency")
    raw_concurrency = _extract_raw_section(content, "concurrency")

    return WorkflowTriggerInfo(
        name=name,
        filename=filename,
        non_call_triggers=non_call_triggers,
        call_inputs=call_inputs,
        call_secrets=call_secrets,
        has_workflow_call=has_workflow_call,
        concurrency=concurrency,
        raw_concurrency=raw_concurrency,
    )


def _render_trigger_value(value: Any, indent: int) -> str:
    """Render a single trigger's configuration value as YAML text.

    :param value: The trigger configuration (None for empty, dict, list, etc.).
    :param indent: Current indentation level in spaces.
    :return: YAML fragment for this trigger value.
    """
    prefix = " " * indent
    if value is None:
        return ""

    if isinstance(value, list):
        lines = []
        for item in value:
            if isinstance(item, dict):
                # Inline dict items like ``{cron: "..."}`` rendered as mapping.
                first = True
                for k, v in item.items():
                    if first:
                        lines.append(f"{prefix}- {k}: {_quote_yaml_value(v)}")
                        first = False
                    else:
                        lines.append(f"{prefix}  {k}: {_quote_yaml_value(v)}")
            else:
                lines.append(f"{prefix}- {_quote_yaml_list_item(item)}")
        return "\n".join(lines)

    if isinstance(value, dict):
        lines = []
        for k, v in value.items():
            if v is None:
                lines.append(f"{prefix}{k}:")
            elif isinstance(v, list):
                lines.append(f"{prefix}{k}:")
                for item in v:
                    if isinstance(item, dict):
                        first = True
                        for dk, dv in item.items():
                            if first:
                                lines.append(
                                    f"{prefix}  - {dk}: {_quote_yaml_value(dv)}"
                                )
                                first = False
                            else:
                                lines.append(
                                    f"{prefix}    {dk}: {_quote_yaml_value(dv)}"
                                )
                    else:
                        lines.append(f"{prefix}  - {_quote_yaml_list_item(item)}")
            elif isinstance(v, dict):
                lines.append(f"{prefix}{k}:")
                for sk, sv in v.items():
                    if sv is None:
                        lines.append(f"{prefix}  {sk}:")
                    elif isinstance(sv, list):
                        lines.append(f"{prefix}  {sk}:")
                        for item in sv:
                            lines.append(f"{prefix}    - {_quote_yaml_list_item(item)}")
                    else:
                        lines.append(f"{prefix}  {sk}: {_quote_yaml_value(sv)}")
            else:
                lines.append(f"{prefix}{k}: {_quote_yaml_value(v)}")
        return "\n".join(lines)

    return f"{prefix}{value}"


def _quote_yaml_value(value: Any) -> str:
    """Quote a YAML value if it needs quoting.

    :param value: A scalar YAML value.
    :return: String representation, quoted if necessary.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        # Quote strings that contain special YAML characters.
        if any(c in value for c in ":#{}[]|>&*!%@`"):
            return f'"{value}"'
    return str(value)


def _quote_yaml_list_item(value: Any) -> str:
    """Quote a YAML list item if it needs quoting.

    :param value: A scalar YAML value used as a list item.
    :return: String representation, quoted if necessary.
    """
    if isinstance(value, str):
        # Quote strings that start with or contain YAML-special characters.
        if any(c in value for c in "*&!%@`#{}[]|>"):
            return f'"{value}"'
    return str(value)


def _render_triggers(triggers: dict[str, Any]) -> str:
    """Render the complete trigger block for a thin caller workflow.

    :param triggers: Dictionary of trigger names to their configurations.
    :return: YAML text for the ``"on":`` block.
    """
    lines = ['"on":']
    for trigger_name, trigger_config in triggers.items():
        if trigger_config is None:
            lines.append(f"  {trigger_name}:")
        else:
            rendered = _render_trigger_value(trigger_config, indent=4)
            if rendered:
                lines.append(f"  {trigger_name}:")
                lines.append(rendered)
            else:
                lines.append(f"  {trigger_name}:")
    return "\n".join(lines)


def generate_thin_caller(
    filename: str,
    repo: str = DEFAULT_REPO,
    version: str = DEFAULT_VERSION,
) -> str:
    """Generate a thin caller workflow for a reusable canonical workflow.

    The generated caller includes all non-``workflow_call`` triggers from the
    canonical workflow, always ensures ``workflow_dispatch`` is present, and
    delegates to the upstream workflow via ``uses:``.

    :param filename: Canonical workflow filename (e.g., ``release.yaml``).
    :param repo: Upstream repository (default: ``kdeldycke/repomatic``).
    :param version: Version reference (default: ``main``).
    :return: Complete YAML content for the thin caller workflow.
    :raises ValueError: If the workflow does not support ``workflow_call``.
    """
    info = extract_trigger_info(filename)

    if not info.has_workflow_call:
        msg = (
            f"{filename} does not define a workflow_call trigger "
            "and cannot be used as a thin caller target."
        )
        raise ValueError(msg)

    # Build trigger dict, ensuring workflow_dispatch is present.
    triggers: dict[str, Any] = {
        trigger_name: trigger_config
        for trigger_name, trigger_config in info.non_call_triggers.items()
    }
    if "workflow_dispatch" not in triggers:
        triggers["workflow_dispatch"] = None

    # Build the YAML content programmatically.
    # Concurrency is intentionally omitted: the reusable workflow's own
    # concurrency block applies when called via ``workflow_call``, so
    # duplicating it in the thin caller would be redundant.
    lines = [
        "---",
        f"name: {info.name}",
        _render_triggers(triggers),
        "",
        "jobs:",
        "",
        f"  {filename.removesuffix('.yaml')}:",
        f"    uses: {repo}/.github/workflows/{filename}@{version}",
    ]

    # Pass only the specific secrets the canonical workflow declares, so
    # downstream callers don't trigger zizmor's ``secrets-inherit`` finding.
    if info.call_secrets:
        lines.append("    secrets:")
        for secret_name in info.call_secrets:
            lines.append(f"      {secret_name}: ${{{{ secrets.{secret_name} }}}}")

    # Trailing newline.
    lines.append("")

    return "\n".join(lines)


def identify_canonical_workflow(
    workflow_path: Path,
    repo: str = DEFAULT_REPO,
) -> str | None:
    """Identify if a workflow is a thin caller for a canonical upstream workflow.

    Scans jobs for a ``uses:`` reference matching the upstream repository pattern.

    :param workflow_path: Path to the workflow file.
    :param repo: Upstream repository to match against.
    :return: Canonical workflow filename, or ``None`` if not a thin caller.
    """
    try:
        content = workflow_path.read_text(encoding="UTF-8")
        data = yaml.safe_load(content)
    except (OSError, yaml.YAMLError):
        return None

    if not isinstance(data, dict):
        return None

    jobs = data.get("jobs")
    if not isinstance(jobs, dict):
        return None

    pattern = re.compile(rf"^{re.escape(repo)}/\.github/workflows/([^@]+)@.+$")

    for job_config in jobs.values():
        if not isinstance(job_config, dict):
            continue
        uses = job_config.get("uses", "")
        match = pattern.match(uses)
        if match:
            return match.group(1)

    return None


# ---------------------------------------------------------------------------
# Lint checks
# ---------------------------------------------------------------------------


def check_has_workflow_dispatch(workflow_path: Path) -> LintResult:
    """Check that a workflow has a ``workflow_dispatch`` trigger.

    :param workflow_path: Path to the workflow file.
    :return: Lint result.
    """
    try:
        content = workflow_path.read_text(encoding="UTF-8")
        data = yaml.safe_load(content)
    except (OSError, yaml.YAMLError) as e:
        return LintResult(
            message=f"{workflow_path.name}: failed to parse: {e}",
            is_issue=True,
            level=AnnotationLevel.ERROR,
        )

    triggers: dict[str, Any] = {}
    if isinstance(data, dict):
        if True in data:
            triggers = data[True] or {}
        elif "on" in data:
            triggers = data["on"] or {}

    if "workflow_dispatch" not in triggers:
        return LintResult(
            message=(f"{workflow_path.name}: missing workflow_dispatch trigger."),
            is_issue=True,
            level=AnnotationLevel.WARNING,
        )

    return LintResult(
        message=f"{workflow_path.name}: has workflow_dispatch trigger.",
        is_issue=False,
    )


def check_version_pinned(
    workflow_path: Path,
    repo: str = DEFAULT_REPO,
) -> LintResult:
    """Check that a thin caller pins to a version tag, not ``@main``.

    :param workflow_path: Path to the workflow file.
    :param repo: Upstream repository to match against.
    :return: Lint result.
    """
    try:
        content = workflow_path.read_text(encoding="UTF-8")
    except OSError as e:
        return LintResult(
            message=f"{workflow_path.name}: failed to read: {e}",
            is_issue=True,
            level=AnnotationLevel.ERROR,
        )

    pattern = re.compile(rf"{re.escape(repo)}/\.github/workflows/[^@]+@main")

    if pattern.search(content):
        return LintResult(
            message=(f"{workflow_path.name}: uses @main instead of a version tag."),
            is_issue=True,
            level=AnnotationLevel.WARNING,
        )

    return LintResult(
        message=f"{workflow_path.name}: version is pinned.",
        is_issue=False,
    )


def check_triggers_match(
    workflow_path: Path,
    canonical_filename: str,
) -> LintResult:
    """Check that a thin caller's triggers match the canonical workflow.

    Verifies that the caller includes all non-``workflow_call`` triggers
    defined in the canonical workflow.

    :param workflow_path: Path to the caller workflow file.
    :param canonical_filename: Filename of the canonical upstream workflow.
    :return: Lint result.
    """
    try:
        content = workflow_path.read_text(encoding="UTF-8")
        data = yaml.safe_load(content)
    except (OSError, yaml.YAMLError) as e:
        return LintResult(
            message=f"{workflow_path.name}: failed to parse: {e}",
            is_issue=True,
            level=AnnotationLevel.ERROR,
        )

    caller_triggers: set[str] = set()
    if isinstance(data, dict):
        raw = data.get(True) or data.get("on") or {}
        caller_triggers = set(raw.keys()) if isinstance(raw, dict) else set()

    info = extract_trigger_info(canonical_filename)
    expected = set(info.non_call_triggers.keys()) | {"workflow_dispatch"}

    missing = expected - caller_triggers
    if missing:
        return LintResult(
            message=(
                f"{workflow_path.name}: missing triggers vs canonical"
                f" {canonical_filename}: {', '.join(sorted(missing))}."
            ),
            is_issue=True,
            level=AnnotationLevel.WARNING,
        )

    return LintResult(
        message=f"{workflow_path.name}: triggers match canonical.",
        is_issue=False,
    )


def check_secrets_passed(
    workflow_path: Path,
    canonical_filename: str,
) -> LintResult:
    """Check that a thin caller passes all required secrets explicitly.

    Verifies that every secret declared by the canonical workflow is forwarded
    by the caller, either via explicit ``secrets:`` mapping or via
    ``secrets: inherit``.

    :param workflow_path: Path to the caller workflow file.
    :param canonical_filename: Filename of the canonical upstream workflow.
    :return: Lint result.
    """
    info = extract_trigger_info(canonical_filename)

    if not info.call_secrets:
        return LintResult(
            message=(
                f"{workflow_path.name}: no secrets required by {canonical_filename}."
            ),
            is_issue=False,
        )

    try:
        content = workflow_path.read_text(encoding="UTF-8")
        data = yaml.safe_load(content)
    except (OSError, yaml.YAMLError) as e:
        return LintResult(
            message=f"{workflow_path.name}: failed to parse: {e}",
            is_issue=True,
            level=AnnotationLevel.ERROR,
        )

    if not isinstance(data, dict):
        return LintResult(
            message=f"{workflow_path.name}: invalid workflow structure.",
            is_issue=True,
            level=AnnotationLevel.ERROR,
        )

    expected = set(info.call_secrets)
    jobs = data.get("jobs") or {}
    for job_config in jobs.values():
        if not isinstance(job_config, dict):
            continue
        job_secrets = job_config.get("secrets")
        # ``secrets: inherit`` forwards everything.
        if job_secrets == "inherit":
            return LintResult(
                message=f"{workflow_path.name}: secrets: inherit is set.",
                is_issue=False,
            )
        if isinstance(job_secrets, dict):
            passed = set(job_secrets)
            missing = expected - passed
            if not missing:
                return LintResult(
                    message=(
                        f"{workflow_path.name}: all secrets"
                        f" passed to {canonical_filename}."
                    ),
                    is_issue=False,
                )
            return LintResult(
                message=(
                    f"{workflow_path.name}: missing secrets for"
                    f" {canonical_filename}:"
                    f" {', '.join(sorted(missing))}."
                ),
                is_issue=True,
                level=AnnotationLevel.WARNING,
            )

    return LintResult(
        message=(
            f"{workflow_path.name}: no secrets passed but"
            f" {canonical_filename} defines secrets."
        ),
        is_issue=True,
        level=AnnotationLevel.WARNING,
    )


def check_concurrency_match(
    workflow_path: Path,
    canonical_filename: str,
) -> LintResult:
    """Check that a thin caller's concurrency block matches the canonical workflow.

    Compares parsed concurrency dicts so formatting differences are ignored.

    :param workflow_path: Path to the caller workflow file.
    :param canonical_filename: Filename of the canonical upstream workflow.
    :return: Lint result.
    """
    info = extract_trigger_info(canonical_filename)

    try:
        content = workflow_path.read_text(encoding="UTF-8")
        data = yaml.safe_load(content)
    except (OSError, yaml.YAMLError) as e:
        return LintResult(
            message=f"{workflow_path.name}: failed to parse: {e}",
            is_issue=True,
            level=AnnotationLevel.ERROR,
        )

    caller_concurrency = data.get("concurrency") if isinstance(data, dict) else None

    if info.concurrency is None:
        # Canonical has no concurrency; caller shouldn't either, but don't flag it.
        return LintResult(
            message=(
                f"{workflow_path.name}: no concurrency required"
                f" by {canonical_filename}."
            ),
            is_issue=False,
        )

    if caller_concurrency is None:
        return LintResult(
            message=(
                f"{workflow_path.name}: missing concurrency block"
                f" (expected by {canonical_filename})."
            ),
            is_issue=True,
            level=AnnotationLevel.WARNING,
        )

    if caller_concurrency != info.concurrency:
        return LintResult(
            message=(
                f"{workflow_path.name}: concurrency block does not match"
                f" canonical {canonical_filename}."
            ),
            is_issue=True,
            level=AnnotationLevel.WARNING,
        )

    return LintResult(
        message=f"{workflow_path.name}: concurrency matches canonical.",
        is_issue=False,
    )


def generate_workflow_header(filename: str) -> str:
    """Return the raw header of a canonical workflow.

    The header is everything before the ``jobs:`` line: ``name``, ``on``
    triggers, ``concurrency``, and any comments.

    :param filename: Canonical workflow filename (e.g., ``tests.yaml``).
    :return: Raw header text.
    :raises FileNotFoundError: If the workflow file is not bundled.
    :raises ValueError: If no ``jobs:`` line is found.
    """
    content = get_data_content(filename)
    return _extract_raw_header(content)


def run_workflow_lint(
    workflow_dir: Path,
    repo: str = DEFAULT_REPO,
    fatal: bool = False,
) -> int:
    """Lint all workflow files in a directory.

    Runs ``check_has_workflow_dispatch`` on all YAML files, and caller-specific
    checks on files identified as thin callers.

    :param workflow_dir: Directory containing workflow YAML files.
    :param repo: Upstream repository to match against.
    :param fatal: If ``True``, return exit code 1 when issues are found.
    :return: Exit code (0 for clean, 1 if fatal and issues found).
    """
    if not workflow_dir.is_dir():
        logging.error(f"Workflow directory not found: {workflow_dir}")
        return 1

    yaml_files = sorted(workflow_dir.glob("*.yaml"))
    if not yaml_files:
        logging.warning(f"No YAML files found in {workflow_dir}")
        return 0

    issues_found = False

    for wf_path in yaml_files:
        # Check 1: workflow_dispatch presence.
        result = check_has_workflow_dispatch(wf_path)
        _emit_lint_result(result)
        if result.is_issue:
            issues_found = True

        # Identify if this is a thin caller.
        canonical = identify_canonical_workflow(wf_path, repo)
        if canonical is None:
            continue

        # Check 2: version pinning.
        result = check_version_pinned(wf_path, repo)
        _emit_lint_result(result)
        if result.is_issue:
            issues_found = True

        # Check 3: trigger match.
        result = check_triggers_match(wf_path, canonical)
        _emit_lint_result(result)
        if result.is_issue:
            issues_found = True

        # Check 4: secrets passed.
        result = check_secrets_passed(wf_path, canonical)
        _emit_lint_result(result)
        if result.is_issue:
            issues_found = True

    if issues_found and fatal:
        return 1
    return 0


def _emit_lint_result(result: LintResult) -> None:
    """Print a lint result and emit a GitHub Actions annotation if needed.

    :param result: The lint result to emit.
    """
    if result.is_issue:
        emit_annotation(result.level, result.message)
        prefix = "⚠" if result.level == AnnotationLevel.WARNING else "✗"
        print(f"{prefix} {result.message}")
    else:
        logging.info(f"✓ {result.message}")


def generate_workflows(
    names: tuple[str, ...],
    output_format: WorkflowFormat,
    version: str,
    repo: str,
    output_dir: Path,
    overwrite: bool,
) -> int:
    """Generate workflow files in the specified format.

    Shared logic for the ``create`` and ``sync`` subcommands.

    :param names: Workflow filenames to generate. Empty tuple means all.
    :param output_format: See :class:`WorkflowFormat` for available formats.
    :param version: Version reference for thin callers.
    :param repo: Upstream repository.
    :param output_dir: Directory to write files to.
    :param overwrite: Whether to overwrite existing files.
    :return: Exit code (0 for success, 1 for errors).
    """
    # Default to all reusable workflows for thin-caller, non-reusable for
    # header-only, all for other modes.
    if not names:
        if output_format == WorkflowFormat.THIN_CALLER:
            names = REUSABLE_WORKFLOWS
        elif output_format == WorkflowFormat.HEADER_ONLY:
            names = tuple(sorted(NON_REUSABLE_WORKFLOWS))
        else:
            names = ALL_WORKFLOW_FILES

    output_dir.mkdir(parents=True, exist_ok=True)

    errors = 0

    for filename in names:
        target = output_dir / filename

        if not overwrite and target.exists():
            logging.error(f"{target} already exists. Use sync to overwrite.")
            errors += 1
            continue

        if output_format == WorkflowFormat.THIN_CALLER:
            if filename in NON_REUSABLE_WORKFLOWS:
                logging.warning(
                    f"Skipping {filename}: no workflow_call trigger"
                    " (not reusable). Use full-copy or symlink mode instead."
                )
                continue

            try:
                content = generate_thin_caller(filename, repo, version)
            except ValueError as e:
                logging.error(str(e))
                errors += 1
                continue

            target.write_text(content, encoding="UTF-8")
            logging.info(f"Generated thin caller: {target}")

        elif output_format == WorkflowFormat.HEADER_ONLY:
            if not target.exists():
                logging.error(
                    f"{target} does not exist. header-only mode requires"
                    " an existing file with a jobs: section."
                )
                errors += 1
                continue

            try:
                canonical_header = generate_workflow_header(filename)
            except (ValueError, FileNotFoundError) as e:
                logging.error(f"Failed to extract header for {filename}: {e}")
                errors += 1
                continue

            existing = target.read_text(encoding="UTF-8")
            jobs_match = re.search(r"^jobs:", existing, re.MULTILINE)
            if jobs_match is None:
                logging.error(f"{target} has no 'jobs:' line to preserve.")
                errors += 1
                continue

            content = canonical_header + existing[jobs_match.start() :]
            target.write_text(content, encoding="UTF-8")
            logging.info(f"Synced header: {target}")

        elif output_format == WorkflowFormat.FULL_COPY:
            try:
                content = export_content(filename)
            except (ValueError, FileNotFoundError) as e:
                logging.error(f"Failed to export {filename}: {e}")
                errors += 1
                continue

            target.write_text(content, encoding="UTF-8")
            logging.info(f"Exported full copy: {target}")

        elif output_format == WorkflowFormat.SYMLINK:
            try:
                # Resolve the bundled data path.
                from importlib.resources import as_file, files

                data_files = files("repomatic.data")
                with as_file(data_files.joinpath(filename)) as source:
                    if not source.exists():
                        logging.error(f"Bundled file not found: {filename}")
                        errors += 1
                        continue
                    source_resolved = source.resolve()

                if target.exists() or target.is_symlink():
                    target.unlink()
                target.symlink_to(source_resolved)
                logging.info(f"Created symlink: {target} -> {source_resolved}")
            except Exception as e:
                logging.error(f"Failed to create symlink for {filename}: {e}")
                errors += 1
                continue

    return 1 if errors else 0
