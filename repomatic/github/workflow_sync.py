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

Downstream repositories consuming reusable workflows from `kdeldycke/repomatic`
manually write caller workflows that often miss triggers like
`workflow_dispatch`. This module provides tools to generate, synchronize, and
lint those callers by parsing the canonical workflow definitions.

See {class}`WorkflowFormat` for available output formats and their behavior.

```{caution}
PyYAML destroys formatting and comments on round-trip. Until we find a
layout-preserving YAML parsing and rendering solution, we use raw text
extraction to manipulate workflow files while preserving formatting and
comments.
```
"""

from __future__ import annotations

import logging
import re
import sys
from dataclasses import dataclass, field
from importlib.resources import as_file, files
from pathlib import Path

import yaml

from .. import __version__
from ..init_project import export_content, get_data_content
from ..registry import (
    ALL_WORKFLOW_FILES,
    DEFAULT_REPO,
    NON_REUSABLE_WORKFLOWS,
    REUSABLE_WORKFLOWS,
    UPSTREAM_SOURCE_GLOB,
    UPSTREAM_SOURCE_PREFIX,
    WORKFLOW_SOURCES,
)
from .actions import AnnotationLevel, emit_annotation

if sys.version_info >= (3, 11):
    from enum import StrEnum
else:
    from backports.strenum import StrEnum

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
    """Sync only the header (`name`, `on`, `concurrency`) from upstream.

    Replaces everything before the `jobs:` line in an existing downstream file
    with the canonical header. The downstream `jobs:` section is preserved.
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
    `name`, `on` triggers, and a `jobs:` section that calls the upstream
    workflow via `workflow_call`. Only works for reusable workflows (those with
    a `workflow_call` trigger).

    When the target file already exists and contains extra jobs beyond the
    managed caller job, those jobs are preserved and appended after the
    regenerated content.
    """


DEFAULT_VERSION: Final[str] = "main" if ".dev" in __version__ else f"v{__version__}"
"""Default version reference for upstream workflows.

For release builds (e.g., `repomatic==5.11.0`), this resolves to the
corresponding tag (`v5.11.0`). For development builds (`5.11.1.dev0`),
it falls back to `main` since the tag does not exist yet.
"""


def _extract_raw_section(content: str, section_name: str) -> str | None:
    """Extract a top-level YAML section as raw text.

    Finds a line matching ``{section_name}:`` at column 0 and returns it along
    with all indented continuation lines (including comments). Returns `None`
    if the section is not found.

    :param content: Full workflow file content.
    :param section_name: Top-level key to extract (e.g., `"concurrency"`).
    :return: Raw text of the section, or `None` if absent.
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
    """Extract everything before the `jobs:` line as raw text.

    :param content: Full workflow file content.
    :return: Raw header text (up to but not including `jobs:`).
    :raises ValueError: If no `jobs:` line is found.
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
    """Workflow display name from the `name:` field."""

    filename: str
    """Workflow filename (e.g., `release.yaml`)."""

    non_call_triggers: dict[str, Any]
    """All triggers except `workflow_call`, preserving their configuration."""

    call_inputs: dict[str, Any]
    """Inputs defined under `workflow_call.inputs`."""

    call_secrets: dict[str, Any]
    """Secrets defined under `workflow_call.secrets`."""

    has_workflow_call: bool
    """Whether the workflow defines a `workflow_call` trigger."""

    concurrency: dict[str, Any] | None
    """Parsed concurrency configuration, or `None` if absent."""

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

    Parses the workflow YAML and separates `workflow_call` configuration from
    other triggers.

    :param filename: Workflow filename (e.g., `release.yaml`).
    :return: Parsed trigger information.
    :raises FileNotFoundError: If the workflow file is not bundled.
    """
    content = get_data_content(filename)
    data = yaml.safe_load(content)

    name = data.get("name", filename)

    # Handle YAML parsing of bare `on` keyword: PyYAML reads bare `on` as
    # boolean `True`, while quoted `"on"` is a string key.
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
                # Recurse to handle arbitrarily nested dicts.
                lines.append(_render_trigger_value(v, indent + 2))
            else:
                lines.append(f"{prefix}{k}: {_quote_yaml_value(v)}")
        return "\n".join(lines)

    return f"{prefix}{value}"


def _quote_yaml_value(value: Any) -> str:
    """Quote a YAML value if it needs quoting.

    Quotes strings that contain special YAML characters.

    :param value: A scalar YAML value.
    :return: String representation, quoted if necessary.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str) and any(c in value for c in ":#{}[]|>&*!%@`"):
        return f'"{value}"'
    return str(value)


def _quote_yaml_list_item(value: Any) -> str:
    """Quote a YAML list item if it needs quoting.

    Quotes strings that start with or contain YAML-special characters.

    :param value: A scalar YAML value used as a list item.
    :return: String representation, quoted if necessary.
    """
    if isinstance(value, str) and any(c in value for c in "*&!%@`#{}[]|>"):
        return f'"{value}"'
    return str(value)


def _render_triggers(triggers: dict[str, Any]) -> str:
    """Render the complete trigger block for a thin caller workflow.

    :param triggers: Dictionary of trigger names to their configurations.
    :return: YAML text for the `"on":` block.
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


@dataclass
class PathsSpec:
    """Bundle of downstream `paths:` adaptation knobs.

    Each field maps to a `[tool.repomatic.workflow]` option.

    :param source_paths: Substituted in for the canonical `repomatic/**` glob
        in every workflow that references it. `None` drops the glob without
        substitution.
    :param extra_paths: Appended to every workflow's `paths:` list (after
        source substitution and `ignore_paths` filtering, before render).
        Skipped for workflows listed in *workflow_paths*.
    :param ignore_paths: Removed from every workflow's `paths:` list by exact
        string match. Skipped for workflows listed in *workflow_paths*.
    :param workflow_paths: Per-workflow override keyed by filename. The value
        is treated as the complete `paths:` list for that workflow; the other
        knobs do not apply.
    """

    source_paths: list[str] | None = None
    extra_paths: list[str] = field(default_factory=list)
    ignore_paths: list[str] = field(default_factory=list)
    workflow_paths: dict[str, list[str]] = field(default_factory=dict)


def _coerce_paths_spec(
    spec: PathsSpec | None,
    source_paths: list[str] | None,
) -> PathsSpec:
    """Build a {class}`PathsSpec` from the legacy *source_paths* arg.

    When *spec* is `None`, returns a fresh spec carrying *source_paths*.
    When *spec* is provided, *source_paths* is ignored.
    """
    if spec is not None:
        return spec
    return PathsSpec(source_paths=source_paths)


def _apply_paths_spec(
    paths: list[str],
    filename: str,
    spec: PathsSpec,
) -> list[str]:
    """Adapt a canonical `paths:` list using the spec's knobs.

    Order of operations (skipped when a per-workflow override is set):

    1. Substitute {data}`UPSTREAM_SOURCE_GLOB` with ``{sp}/**`` for each
       *source_paths* entry, drop other {data}`UPSTREAM_SOURCE_PREFIX` paths.
    2. Strip *ignore_paths* entries (exact string match).
    3. Append *extra_paths* (deduplicated, order-preserving).

    :param paths: Original paths list from a canonical workflow trigger.
    :param filename: Workflow filename (e.g., `tests.yaml`) used to look up
        per-workflow overrides in *spec*.
    :param spec: Active paths spec.
    :return: Adapted paths list. Empty when every entry is dropped.
    """
    if filename in spec.workflow_paths:
        return list(spec.workflow_paths[filename])

    result = _substitute_source_paths(paths, spec.source_paths or [])

    if spec.ignore_paths:
        ignored = set(spec.ignore_paths)
        result = [p for p in result if p not in ignored]

    for entry in spec.extra_paths:
        if entry not in result:
            result.append(entry)

    return result


def _adapt_trigger_paths(
    trigger_config: dict[str, Any],
    filename: str,
    spec: PathsSpec,
) -> dict[str, Any]:
    """Adapt `paths` and `paths-ignore` in a trigger for downstream use.

    `paths` runs through the full {func}`_apply_paths_spec` pipeline.
    `paths-ignore` only receives source-path substitution; the global
    `extra_paths`/`ignore_paths` knobs and per-workflow overrides target
    `paths:` (the trigger gate), not exclusion lists.

    :param trigger_config: Trigger configuration dict (e.g., push config).
    :param filename: Workflow filename for per-workflow override lookup.
    :param spec: Active paths spec.
    :return: New trigger config dict with adapted path filters.
    """
    result = dict(trigger_config)
    if "paths" in result:
        adapted = _apply_paths_spec(result["paths"], filename, spec)
        if adapted:
            result["paths"] = adapted
        else:
            del result["paths"]
    if "paths-ignore" in result:
        adapted_ignore = _substitute_source_paths(
            result["paths-ignore"], spec.source_paths or []
        )
        if adapted_ignore:
            result["paths-ignore"] = adapted_ignore
        else:
            del result["paths-ignore"]
    return result


def _substitute_source_paths(
    paths: list[str],
    source_paths: list[str],
) -> list[str]:
    """Replace upstream source directory paths with downstream source paths.

    For each path in the canonical workflow's `paths:` list:

    - {data}`UPSTREAM_SOURCE_GLOB` (`repomatic/**`) is replaced with
      ``{source}/**`` for each entry in *source_paths*; when *source_paths*
      is empty the glob is dropped entirely.
    - Other paths starting with {data}`UPSTREAM_SOURCE_PREFIX` are dropped
      (upstream-specific files like `repomatic/data/renovate.json5`).
    - All other paths (universal paths like `pyproject.toml`, `tests/**`)
      are kept as-is.

    :param paths: Original paths list from a canonical workflow trigger.
    :param source_paths: Downstream source directory names. Empty list
        drops the upstream source glob without substitution.
    :return: New paths list with substitutions applied.
    """
    result: list[str] = []
    for path in paths:
        if path == UPSTREAM_SOURCE_GLOB:
            result.extend(f"{sp}/**" for sp in source_paths)
        elif path.startswith(UPSTREAM_SOURCE_PREFIX):
            # Drop upstream-specific paths.
            continue
        else:
            result.append(path)
    return result


def generate_thin_caller(
    filename: str,
    repo: str = DEFAULT_REPO,
    version: str = DEFAULT_VERSION,
    source_paths: list[str] | None = None,
    commit_sha: str | None = None,
    paths_spec: PathsSpec | None = None,
) -> str:
    """Generate a thin caller workflow for a reusable canonical workflow.

    The generated caller mirrors the canonical workflow's non-`workflow_call`
    triggers verbatim and delegates to the upstream workflow via `uses:`.
    `workflow_dispatch` is not injected: workflows that should expose manual
    dispatch declare it in the canonical definition.

    Canonical `paths:` filters are adapted via *paths_spec* (see
    {class}`PathsSpec`). The legacy *source_paths* kwarg builds a spec
    automatically when *paths_spec* is `None`.

    When *commit_sha* is provided, the `uses:` reference is SHA-pinned
    (`@sha # version`) matching Renovate's pin format. This eliminates
    Renovate's initial "pin dependencies" PR on downstream repos.

    :param filename: Canonical workflow filename (e.g., `release.yaml`).
    :param repo: Upstream repository (default: `kdeldycke/repomatic`).
    :param version: Version reference (default: `main`).
    :param source_paths: Backward-compat shortcut; ignored when *paths_spec*
        is provided.
    :param commit_sha: Full 40-character commit SHA for the version tag.
        When provided, produces `@sha # version`. When `None`, produces
        `@version`.
    :param paths_spec: Full paths-adaptation spec; supersedes *source_paths*.
    :return: Complete YAML content for the thin caller workflow.
    :raises ValueError: If the workflow does not support `workflow_call`.
    """
    # release.yaml is a multi-job caller (build lane + publish-pypi + engine
    # lane), not a single thin delegation. Generate it by copying the canonical
    # caller and rewriting its local `uses:` refs. See _generate_release_caller.
    if filename == "release.yaml":
        return _generate_release_caller(
            repo=repo, version=version, commit_sha=commit_sha
        )

    spec = _coerce_paths_spec(paths_spec, source_paths)
    # The reusable to read and reference: `filename` itself for every thin
    # caller. (WORKFLOW_SOURCES still maps release.yaml to the engine for
    # reference, but release.yaml is generated above, not here.)
    source = WORKFLOW_SOURCES.get(filename, filename)
    info = extract_trigger_info(source)

    if not info.has_workflow_call:
        msg = (
            f"{filename} does not define a workflow_call trigger "
            "and cannot be used as a thin caller target."
        )
        raise ValueError(msg)

    # Mirror canonical triggers verbatim; do not synthesize workflow_dispatch.
    triggers: dict[str, Any] = {}
    for trigger_name, trigger_config in info.non_call_triggers.items():
        if isinstance(trigger_config, dict):
            trigger_config = _adapt_trigger_paths(trigger_config, filename, spec)
        triggers[trigger_name] = trigger_config

    # Build the YAML content programmatically.
    # Concurrency is intentionally omitted: the reusable workflow's own
    # concurrency block applies when called via `workflow_call`, so
    # duplicating it in the thin caller would be redundant.
    if commit_sha:
        uses_ref = f"{commit_sha} # {version}"
    else:
        uses_ref = version
    main_job = filename.removesuffix(".yaml")
    lines = [
        "---",
        f"name: {info.name}",
        _render_triggers(triggers),
        "",
        "jobs:",
        "",
        f"  {main_job}:",
        f"    uses: {repo}/.github/workflows/{source}@{uses_ref}",
    ]

    # Pass only the specific secrets the canonical workflow declares, so
    # downstream callers don't trigger zizmor's `secrets-inherit` finding.
    if info.call_secrets:
        lines.append("    secrets:")
        lines.extend(
            f"      {secret_name}: ${{{{ secrets.{secret_name} }}}}"
            for secret_name in info.call_secrets
        )

    # Trailing newline.
    lines.append("")

    return "\n".join(lines)


def _extract_raw_job(content: str, job_name: str) -> str | None:
    """Extract a single job mapping from a workflow as raw text.

    Finds the ``  {job_name}:`` line at the two-space job indent and returns it
    with all of the job's indented body, stopping at the next sibling job key
    (or end of file). Preserves comments and formatting, consistent with the
    rest of this module.

    :param content: Full workflow file content.
    :param job_name: Job key to extract (e.g., ``"publish-pypi"``).
    :return: Raw text of the job, or ``None`` if the job is absent.
    """
    key = f"  {job_name}:"
    lines = content.split("\n")
    start = next(
        (i for i, ln in enumerate(lines) if ln == key or ln.startswith(key + " ")),
        None,
    )
    if start is None:
        return None

    result = [lines[start]]
    for line in lines[start + 1 :]:
        stripped = line.strip()
        # A sibling job key sits at exactly the two-space job indent; anything
        # more indented is this job's body. A dedent to column 0 (the next
        # top-level key) also ends the jobs block.
        sibling = (
            line.startswith("  ")
            and not line.startswith("   ")
            and bool(stripped)
            and not stripped.startswith("#")
        )
        if sibling or (line and not line[0].isspace()):
            break
        result.append(line)

    while result and not result[-1].strip():
        result.pop()
    return "\n".join(result)


_LOCAL_PUBLISH_PYPI_ACTION: Final[str] = "./.github/actions/publish-pypi"
"""Relative composite-action ref the canonical `release.yaml` dogfoods.

The canonical entry runs the in-tree action via this local ref (checking
itself out first). :func:`_render_publish_pypi_job` rewrites it to the pinned
cross-repo form (`{repo}/.github/actions/publish-pypi@{ref}`) for downstream
callers, whose OIDC `job_workflow_ref` must resolve to their own release.yaml.
"""


def _render_publish_pypi_job(
    repo: str,
    version: str,
    commit_sha: str | None,
) -> list[str]:
    """Render the caller-side `publish-pypi` job for downstream `release.yaml`.

    The job body is sourced from the canonical `release.yaml` entry (bundled
    under `repomatic/data/`), then reshaped for downstream use:

    - **Drop the leading `actions/checkout` step.** The canonical entry checks
      itself out to run the action from a local `./` ref (dogfooding); a
      downstream caller fetches the cross-repo composite action automatically
      and needs no checkout. Dropping it also keeps the generated workflow free
      of third-party action SHA pins, which would be invisible to Renovate
      (whose scan sees `.yaml`, not this `.py`).
    - **Rewrite the local action ref** :data:`_LOCAL_PUBLISH_PYPI_ACTION` to the
      pinned cross-repo form so the caller's OIDC `job_workflow_ref` resolves to
      its own `release.yaml` (see pypi/warehouse#11096).

    The runner is carried through unchanged: downstream callers run on the same
    `ubuntu-slim` repomatic uses. If a downstream repo turns out to need a
    fuller image, that surfaces as a failure to revisit then.

    Sourcing the body from a `.yaml` file (rather than building it in Python)
    keeps any future third-party SHA pin in the job Renovate-visible.

    :param repo: Upstream repository owning the composite action.
    :param version: Version reference for the action ref.
    :param commit_sha: Optional 40-character SHA for pin-style refs (renders
        as `@sha # version` like Renovate).
    :return: YAML lines (without trailing blank).
    :raises RuntimeError: If the canonical `release.yaml` no longer exposes a
        `publish-pypi` job shaped the way this renderer expects (missing job,
        `steps:` key, or the local action ref), meaning the entry changed in a
        way the renderer was not updated to handle.
    """
    if commit_sha:
        action_ref = f"{commit_sha} # {version}"
    else:
        action_ref = version
    target_ref = f"{repo}/.github/actions/publish-pypi@{action_ref}"

    job = _extract_raw_job(get_data_content("release.yaml"), "publish-pypi")
    if job is None:
        msg = (
            "Canonical release.yaml exposes no 'publish-pypi' job; the entry"
            " changed in a way _render_publish_pypi_job was not updated to handle."
        )
        raise RuntimeError(msg)

    lines = job.split("\n")
    steps_idx = next(
        (i for i, line in enumerate(lines) if line.strip() == "steps:"), None
    )
    anchor_idx = next(
        (i for i, line in enumerate(lines) if _LOCAL_PUBLISH_PYPI_ACTION in line),
        None,
    )
    if steps_idx is None or anchor_idx is None or anchor_idx <= steps_idx:
        msg = (
            "Canonical release.yaml 'publish-pypi' job lacks a 'steps:' key or"
            f" the {_LOCAL_PUBLISH_PYPI_ACTION!r} step; the entry changed in a"
            " way _render_publish_pypi_job was not updated to handle."
        )
        raise RuntimeError(msg)

    # Keep the job header through `steps:`, drop the checkout step(s) preceding
    # the action, then keep the action step onward, rewriting the local ref to
    # the pinned cross-repo form. The runner is carried through unchanged.
    rebuilt = "\n".join(lines[: steps_idx + 1] + lines[anchor_idx:])
    rebuilt = rebuilt.replace(_LOCAL_PUBLISH_PYPI_ACTION, target_ref)
    return ["", *rebuilt.split("\n")]


def _rewrite_workflow_uses(job_text: str, repo: str, uses_ref: str) -> str:
    """Rewrite a job's local reusable-workflow `uses:` to the pinned cross-repo form.

    `uses: ./.github/workflows/X.yaml` becomes
    `uses: {repo}/.github/workflows/X.yaml@{uses_ref}`. All other lines pass
    through unchanged. A function replacement avoids `re` interpreting a `#` or
    digit in *uses_ref* as backreference syntax.

    :param job_text: Raw text of a single job mapping.
    :param repo: Upstream repository owning the reusable workflow.
    :param uses_ref: Ref suffix (`version`, or `sha # version` for pins).
    :return: Job text with its local workflow `uses:` rewritten.
    """
    return re.sub(
        r"(uses:\s*)\./\.github/workflows/(\S+)",
        lambda m: f"{m.group(1)}{repo}/.github/workflows/{m.group(2)}@{uses_ref}",
        job_text,
    )


def _strip_comment_lines(text: str) -> str:
    """Drop whole-line comments from raw job text.

    Used for the `uses:`-only `build` and `release` jobs of the release caller,
    whose upstream comments describe repomatic's own dogfooding (local refs) and
    would mislead once rewritten to pinned cross-repo refs downstream. These jobs
    carry no `run:` blocks, so no inline-comment content is at risk.
    """
    return "\n".join(
        line for line in text.split("\n") if not line.lstrip().startswith("#")
    )


def _generate_release_caller(
    repo: str,
    version: str,
    commit_sha: str | None,
) -> str:
    """Generate the downstream `release.yaml` caller from the canonical entry.

    The canonical `release.yaml` (bundled, repomatic's own self-release entry) is
    a multi-job caller: a `build` lane call, the OIDC `publish-pypi` job, and a
    `release` engine call. Downstream repos get the same shape with:

    - **Standard release triggers synthesized** (`push` to `main` +
      `workflow_dispatch`), so a dogfooding-only trigger added upstream never
      leaks downstream.
    - **Local `uses: ./` refs rewritten** to the pinned cross-repo form
      (`{repo}/.github/workflows/{name}@{ref}`) for the `build` and `release`
      lanes, with their repomatic-specific comments stripped.
    - **The `publish-pypi` job reshaped** by :func:`_render_publish_pypi_job`
      (checkout dropped, action ref pinned). Keeping it in `release.yaml` is what
      makes the OIDC `job_workflow_ref` resolve to each repo's own `release.yaml`
      (see pypi/warehouse#11096).

    Copying the rest keeps the `build`/`publish-pypi`/`release` wiring
    (`needs: build`, secrets) in one canonical source instead of synthesizing it.

    :param repo: Upstream repository owning the reusable workflows and action.
    :param version: Version reference for the pinned refs.
    :param commit_sha: Optional 40-character SHA for pin-style refs (renders as
        `@sha # version` like Renovate).
    :return: Complete YAML content for the downstream `release.yaml` caller.
    :raises RuntimeError: If the canonical `release.yaml` no longer exposes the
        expected `build` and `release` jobs, meaning the entry changed in a way
        this renderer was not updated to handle.
    """
    content = get_data_content("release.yaml")
    info = extract_trigger_info("release.yaml")
    uses_ref = f"{commit_sha} # {version}" if commit_sha else version

    triggers: dict[str, Any] = {
        "workflow_dispatch": None,
        "push": {"branches": ["main"]},
    }

    build_job = _extract_raw_job(content, "build")
    release_job = _extract_raw_job(content, "release")
    if build_job is None or release_job is None:
        msg = (
            "Canonical release.yaml is missing its 'build' or 'release' job; the"
            " entry changed in a way _generate_release_caller was not updated to"
            " handle."
        )
        raise RuntimeError(msg)

    lines = [
        "---",
        f"name: {info.name}",
        _render_triggers(triggers),
        "",
        "jobs:",
        "",
        *_rewrite_workflow_uses(
            _strip_comment_lines(build_job), repo, uses_ref
        ).split("\n"),
    ]
    lines.extend(
        _render_publish_pypi_job(repo=repo, version=version, commit_sha=commit_sha)
    )
    lines.append("")
    lines.extend(
        _rewrite_workflow_uses(
            _strip_comment_lines(release_job), repo, uses_ref
        ).split("\n")
    )
    lines.append("")

    return "\n".join(lines)


def identify_canonical_workflow(
    workflow_path: Path,
    repo: str = DEFAULT_REPO,
) -> str | None:
    """Identify if a workflow is a thin caller for a canonical upstream workflow.

    Scans jobs for a `uses:` reference matching the upstream repository pattern.

    :param workflow_path: Path to the workflow file.
    :param repo: Upstream repository to match against.
    :return: Canonical workflow filename, or `None` if not a thin caller.
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

    # A multi-lane caller (release.yaml: build + engine) references several
    # upstream workflows. Return the LAST match: for release.yaml that is the
    # engine lane, which declares the secrets, so check_secrets_passed validates
    # the meaningful lane. Single-lane thin callers have exactly one match, so
    # last == first and behavior is unchanged.
    canonical: str | None = None
    for job_config in jobs.values():
        if not isinstance(job_config, dict):
            continue
        match = pattern.match(job_config.get("uses", ""))
        if match:
            canonical = match.group(1)

    return canonical


def extract_extra_jobs(
    content: str,
    repo: str = DEFAULT_REPO,
) -> str:
    """Extract extra downstream jobs from an existing thin-caller workflow.

    Parses the file with YAML to identify the managed thin-caller job (the one
    whose `uses:` references the upstream repository), then returns all raw
    text after that job: blank lines, comments, and additional job definitions.

    Uses raw text slicing (not YAML round-tripping) to preserve formatting and
    comments, consistent with the rest of the module.

    :param content: Full workflow file content.
    :param repo: Upstream repository to match against.
    :return: Raw text of extra jobs (empty string when there are none).
    """
    # Identify all managed job keys via YAML parsing. A job is "managed" when
    # its body references the upstream repo: either at the job-level `uses:`
    # (reusable workflow) or at any `steps[*].uses:` (composite action like
    # `kdeldycke/repomatic/.github/actions/publish-pypi`). The last managed
    # job in document order is the boundary; everything after it is extra.
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError:
        return ""
    if not isinstance(data, dict):
        return ""
    jobs = data.get("jobs")
    if not isinstance(jobs, dict):
        return ""

    upstream_pattern = re.compile(rf"{re.escape(repo)}/\.github/(workflows|actions)/")
    managed_keys: list[str] = []
    for key, config in jobs.items():
        if not isinstance(config, dict):
            continue
        if upstream_pattern.search(config.get("uses", "")):
            managed_keys.append(str(key))
            continue
        steps = config.get("steps") or []
        if isinstance(steps, list) and any(
            isinstance(step, dict) and upstream_pattern.search(step.get("uses", ""))
            for step in steps
        ):
            managed_keys.append(str(key))
    if not managed_keys:
        return ""

    # In raw text, walk past each managed job body. Find each job header line
    # then advance through its body (4+ space indent). Use the last managed
    # job's boundary as the cutoff for extras.
    all_lines = content.split("\n")
    last_body_idx = -1
    for managed_key in managed_keys:
        managed_prefix = f"  {managed_key}:"
        managed_idx = None
        for i, line in enumerate(all_lines):
            if i <= last_body_idx:
                continue
            if line == managed_prefix or line.startswith(managed_prefix + " "):
                managed_idx = i
                break
        if managed_idx is None:
            continue
        body_idx = managed_idx
        for i in range(managed_idx + 1, len(all_lines)):
            line = all_lines[i]
            if line.startswith("    "):
                body_idx = i
            elif line == "":
                continue
            else:
                break
        last_body_idx = body_idx

    if last_body_idx < 0:
        return ""

    # Everything after the last managed job body is extra content.
    extra_start = last_body_idx + 1
    if extra_start >= len(all_lines):
        return ""

    extra = "\n".join(all_lines[extra_start:])
    if not extra.strip():
        return ""
    return extra


# ---------------------------------------------------------------------------
# Lint checks
# ---------------------------------------------------------------------------


def check_has_workflow_dispatch(workflow_path: Path) -> LintResult:
    """Check that a workflow has a `workflow_dispatch` trigger.

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
    """Check that a thin caller pins to a version tag, not `@main`.

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

    Verifies that the caller includes all non-`workflow_call` triggers
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
    expected = set(info.non_call_triggers.keys())

    missing = expected - caller_triggers
    # When the canonical defines no non-call triggers, the caller must define
    # its own (e.g. release.yaml callers synthesize push + workflow_dispatch
    # because _release-engine.yaml is call-only in the canonical repo).
    # Only check for extras when the canonical actually declares triggers.
    extra = (caller_triggers - expected - {"workflow_call"}) if expected else set()
    problems: list[str] = []
    if missing:
        problems.append(f"missing: {', '.join(sorted(missing))}")
    if extra:
        problems.append(f"extra: {', '.join(sorted(extra))}")
    if problems:
        return LintResult(
            message=(
                f"{workflow_path.name}: triggers diverge from canonical"
                f" {canonical_filename} ({'; '.join(problems)})."
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
    by the caller, either via explicit `secrets:` mapping or via
    `secrets: inherit`.

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
        # `secrets: inherit` forwards everything.
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


_PATHS_BLOCK_RE = re.compile(
    r"^([ \t]*)paths:[ \t]*\n((?:\1[ \t]+- [^\n]*\n)+)",
    re.MULTILINE,
)
"""Match a `paths:` block and capture its key indent and entry lines.

Group 1 is the indent of the `paths:` key (assumes entries indent further
with the same prefix). Group 2 is the contiguous block of entry lines.
Inline comments inside the entry block would terminate the match early.
"""


def generate_workflow_header(
    filename: str,
    source_paths: list[str] | None = None,
    paths_spec: PathsSpec | None = None,
) -> str:
    """Return the raw header of a canonical workflow.

    The header is everything before the `jobs:` line: `name`, `on`
    triggers, `concurrency`, and any comments.

    Each `paths:` block in the header is rewritten using *paths_spec*:
    upstream source references substituted via *source_paths*, optional
    extras appended, ignored entries stripped, or replaced wholesale via
    a per-workflow override (see {class}`PathsSpec`). When the resulting
    list is empty, the entire `paths:` block is removed. Comments outside
    the rewritten blocks are preserved verbatim; comments inside an
    entry block are not supported.

    :param filename: Canonical workflow filename (e.g., `tests.yaml`).
    :param source_paths: Backward-compat shortcut; ignored when *paths_spec*
        is provided.
    :param paths_spec: Full paths-adaptation spec; supersedes *source_paths*.
    :return: Raw header text.
    :raises FileNotFoundError: If the workflow file is not bundled.
    :raises ValueError: If no `jobs:` line is found.
    """
    spec = _coerce_paths_spec(paths_spec, source_paths)
    content = get_data_content(filename)
    header = _extract_raw_header(content)

    def _rewrite(match: re.Match[str]) -> str:
        key_indent = match.group(1)
        body = match.group(2)
        entry_indent = ""
        # Track each entry's original quote char (or "" when unquoted) so the
        # rewritten block round-trips with the canonical style. Substitutions
        # and additions inherit the dominant style of the canonical block.
        unquoted: list[str] = []
        quotes: list[str] = []
        for line in body.splitlines():
            stripped = line.lstrip()
            if not stripped.startswith("- "):
                continue
            if not entry_indent:
                entry_indent = line[: len(line) - len(stripped)]
            value, quote = _split_yaml_quote(stripped[2:].strip())
            unquoted.append(value)
            quotes.append(quote)
        adapted = _apply_paths_spec(unquoted, filename, spec)
        if not adapted:
            return ""
        if not entry_indent:
            entry_indent = key_indent + "  "
        # Pick the dominant canonical quote style for new entries; use the
        # original quote when an entry was preserved by value.
        quote_by_value = dict(zip(unquoted, quotes, strict=False))
        canonical_quote = next((q for q in quotes if q), "")
        new_lines = []
        for entry in adapted:
            quote = quote_by_value.get(entry, canonical_quote)
            rendered = f"{quote}{entry}{quote}" if quote else entry
            new_lines.append(f"{entry_indent}- {rendered}\n")
        return f"{key_indent}paths:\n{''.join(new_lines)}"

    return _PATHS_BLOCK_RE.sub(_rewrite, header)


def _split_yaml_quote(scalar: str) -> tuple[str, str]:
    """Split a YAML scalar from its surrounding quote.

    :param scalar: Raw scalar text as it appears in the YAML source.
    :return: ``(value, quote_char)`` where *quote_char* is `"` or `'` for
        quoted scalars and the empty string for plain ones.
    """
    if len(scalar) >= 2 and scalar[0] == scalar[-1] and scalar[0] in ('"', "'"):
        return scalar[1:-1], scalar[0]
    return scalar, ""


def run_workflow_lint(
    workflow_dir: Path,
    repo: str = DEFAULT_REPO,
    fatal: bool = False,
) -> int:
    """Lint all workflow files in a directory.

    For thin callers (workflows that delegate to a canonical upstream workflow
    via `uses:`), runs caller-specific checks: version pinning, trigger match,
    and secrets passed. For standalone workflows, runs
    {func}`check_has_workflow_dispatch` to flag missing manual triggers.

    Thin callers are exempt from {func}`check_has_workflow_dispatch` because
    {func}`check_triggers_match` is authoritative: a thin caller mirrors its
    canonical workflow exactly, and some canonical workflows (e.g.,
    `cancel-runs.yaml`) intentionally lack `workflow_dispatch`.

    :param workflow_dir: Directory containing workflow YAML files.
    :param repo: Upstream repository to match against.
    :param fatal: If `True`, return exit code 1 when issues are found.
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
        canonical = identify_canonical_workflow(wf_path, repo)

        if canonical is None:
            # Standalone workflow: enforce manual-dispatch convention.
            result = check_has_workflow_dispatch(wf_path)
            _emit_lint_result(result)
            if result.is_issue:
                issues_found = True
            continue

        # Thin caller: trigger match is authoritative, so skip the
        # standalone workflow_dispatch check.
        for result in (
            check_version_pinned(wf_path, repo),
            check_triggers_match(wf_path, canonical),
            check_secrets_passed(wf_path, canonical),
        ):
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
    source_paths: list[str] | None = None,
    commit_sha: str | None = None,
    paths_spec: PathsSpec | None = None,
) -> int:
    """Generate workflow files in the specified format.

    Shared logic for the `create` and `sync` subcommands.

    :param names: Workflow filenames to generate. Empty tuple means all.
    :param output_format: See {class}`WorkflowFormat` for available formats.
    :param version: Version reference for thin callers.
    :param repo: Upstream repository.
    :param output_dir: Directory to write files to.
    :param overwrite: Whether to overwrite existing files.
    :param source_paths: Backward-compat shortcut; ignored when *paths_spec*
        is provided.
    :param commit_sha: Full 40-character commit SHA for SHA-pinned
        `uses:` references. Passed through to {func}`generate_thin_caller`.
    :param paths_spec: Full paths-adaptation spec; supersedes *source_paths*.
    :return: Exit code (0 for success, 1 for errors).
    """
    spec = _coerce_paths_spec(paths_spec, source_paths)
    # Default to all reusable workflows for thin-caller, non-reusable for
    # header-only, all for other modes.
    names_defaulted = not names
    if not names:
        if output_format == WorkflowFormat.THIN_CALLER:
            names = REUSABLE_WORKFLOWS
        elif output_format == WorkflowFormat.HEADER_ONLY:
            names = tuple(sorted(NON_REUSABLE_WORKFLOWS))
        else:
            names = ALL_WORKFLOW_FILES

    output_dir.mkdir(parents=True, exist_ok=True)

    # For header-only with defaulted names, filter to existing files.
    # Downstream repos may not have all non-reusable workflows.
    if names_defaulted and output_format == WorkflowFormat.HEADER_ONLY:
        names = tuple(n for n in names if (output_dir / n).exists())

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
                content = generate_thin_caller(
                    filename,
                    repo,
                    version,
                    paths_spec=spec,
                    commit_sha=commit_sha,
                )
            except ValueError as e:
                logging.error(str(e))
                errors += 1
                continue

            # Preserve extra downstream jobs from the existing file.
            if target.exists():
                extra = extract_extra_jobs(target.read_text(encoding="UTF-8"), repo)
                if extra:
                    content += extra

            target.write_text(content, encoding="UTF-8")
            logging.info(f"Generated thin caller: {target}")

        elif output_format == WorkflowFormat.HEADER_ONLY:
            if not target.exists():
                logging.warning(f"{target} does not exist. Skipping header-only sync.")
                continue

            try:
                canonical_header = generate_workflow_header(filename, paths_spec=spec)
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
            except OSError as e:
                logging.error(f"Failed to create symlink for {filename}: {e}")
                errors += 1
                continue

    return 1 if errors else 0
