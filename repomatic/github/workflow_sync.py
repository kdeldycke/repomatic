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

{func}`render_thin_caller_for_target` is the single entry point that turns a
canonical workflow into a downstream file on disk; `repomatic init` drives it.

Generating and reshaping workflow content in Python, rather than
hand-maintaining YAML, keeps logic out of the platform-specific GitHub Actions
surface: a tested generator that fails loudly beats a static YAML artifact that
can silently drift, and the smaller GHA surface eases a future migration to
another CI platform. `_render_publish_pypi_job` derives each downstream
`publish-pypi` job from the canonical `release.yaml` this way.

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
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

import yaml

from .. import __version__
from ..bundle import get_data_content
from ..config import Config
from ..registry import (
    DEFAULT_REPO,
    UPSTREAM_SOURCE_GLOB,
    UPSTREAM_SOURCE_PREFIX,
    WORKFLOW_SOURCES,
)
from ..version_sync import min_release_age_days
from .actions import AnnotationLevel, emit_annotation

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import Any, Final


def cooldown_env_block() -> str:
    """Render the supply-chain cooldown `env:` block every workflow carries.

    Rendered from {attr}`~repomatic.config.Config.minimum_release_age` rather
    than written by hand, so the literal in the YAML has exactly one source. The
    same text is asserted verbatim against every checked-in workflow by
    `tests/test_workflows.py`, and emitted into the downstream `release.yaml`
    caller by {func}`_generate_release_caller`.

    ```{caution}
    The comment travels into every downstream repository, so it must read true
    there too. It deliberately does **not** name `tests/test_workflows.py`: that
    file exists only here, and a synced copy would point its readers at a path
    they do not have. Keep any wording added below equally context-free, and
    name a repomatic-private path only in a comment that never ships.
    ```

    ```{note}
    A workflow-level `env:` block cannot reference `needs`, which is why the
    window is a literal here instead of a `metadata` job output: the `metadata`
    job runs `uvx` to compute its own outputs, so anything sourced from it would
    leave that bootstrap install ungated. See `claude.md` § Cooldown on every
    install.
    ```

    :return: The comment and `env:` mapping, newline-terminated, ready to splice
        above a workflow's `jobs:` line.
    """
    window = Config.minimum_release_age
    return (
        "# Supply-chain cooldown: no package published within the window can be"
        " resolved by\n"
        "# any command in this workflow. Set here, not per command, so it also"
        " covers the\n"
        "# `metadata` bootstrap and any step added later; a workflow-level `env:`"
        " cannot\n"
        "# reference `needs`, so the window is a literal kept equal to"
        " `[tool.repomatic]\n"
        "# minimum-release-age`. repomatic's own test suite enforces that"
        " upstream; a\n"
        "# synced copy is kept in step by hand. Deliberate bypasses are"
        " per-package CLI\n"
        "# flags (`--exclude-newer-package`, `--min-release-age-exclude`).\n"
        "# See claude.md for the rationale.\n"
        "env:\n"
        f"  NPM_CONFIG_MIN_RELEASE_AGE: {min_release_age_days(window)}\n"
        f'  UV_EXCLUDE_NEWER: "{window}"\n'
    )


PERMISSION_RANK: Final[dict[str, int]] = {"none": 0, "read": 1, "write": 2}
"""Relative strength of the `permissions:` levels GitHub accepts.

Used to union the same scope granted at different levels across the jobs of a
canonical workflow, keeping the most permissive one.
"""


DEFAULT_VERSION: Final[str] = "main" if ".dev" in __version__ else f"v{__version__}"
"""Default version reference for upstream workflows.

For release builds (e.g., `repomatic==5.11.0`), this resolves to the
corresponding tag (`v5.11.0`). For development builds (`5.11.1.dev0`),
it falls back to `main` since the tag does not exist yet.
"""


def _pin_ref(version: str, commit_sha: str | None) -> str:
    """Build the `@`-suffix of a pinned `uses:` reference.

    Returns ``{sha} # {version}`` (the SHA-pin-with-comment format) when
    *commit_sha* is provided, otherwise the bare *version*. `sync-action-pins`
    keeps these pins current. Shared by every `uses:` renderer
    (thin callers, the `publish-pypi` action, the release lanes) so the pin
    format lives in one place.

    :param version: Version reference (e.g., `v5.11.0` or `main`).
    :param commit_sha: Full 40-character commit SHA, or `None` for an unpinned
        ref.
    :return: The ref suffix to place after `@`.
    """
    return f"{commit_sha} # {version}" if commit_sha else version


def _extract_raw_section(content: str, section_name: str) -> str | None:
    """Extract a top-level YAML section as raw text.

    Finds a line matching ``{section_name}:`` at column 0 and returns it along
    with all indented continuation lines (including comments). Returns `None`
    if the section is not found.

    A trailing run of column-0 comment lines is dropped: by YAML convention a
    comment block sitting flush against the next top-level key documents *that*
    key, so keeping it here would duplicate it into every copy of this section
    and orphan it from the block it explains.

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

    # Strip trailing blank lines, then the comment header of the next section.
    while result and not result[-1].strip():
        result.pop()
    while len(result) > 1 and result[-1].startswith("#"):
        result.pop()
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


def workflow_triggers(data: object) -> dict[str, Any]:
    """Extract a parsed workflow's `on:` mapping.

    ```{note}
    PyYAML follows YAML 1.1, where a bare `on` key parses as the boolean `True`
    while a quoted `"on"` stays a string. Both spellings occur in the wild, so
    every reader of a workflow's triggers has to try the boolean key first and
    the string key second. Resolving that here once keeps the quirk from being
    re-remembered at each call site.
    ```

    :param data: The result of `yaml.safe_load` on a workflow file.
    :return: The trigger mapping, empty when *data* is not a mapping or declares
        no triggers.
    """
    if not isinstance(data, dict):
        return {}
    triggers = data.get(True, data.get("on"))
    return triggers if isinstance(triggers, dict) else {}


def _parse_workflow(workflow_path: Path) -> dict[str, Any] | LintResult:
    """Read and parse a workflow file, or return the lint result for the failure.

    Every lint check opens by loading the file it inspects and reports the same
    error when that fails. Returning the {class}`LintResult` in place of the data
    lets each check start with a two-line guard instead of its own `try`.

    :param workflow_path: Path to the workflow file.
    :return: The parsed mapping, or the {class}`LintResult` the caller returns
        as-is.
    """
    try:
        data = yaml.safe_load(workflow_path.read_text(encoding="UTF-8"))
    except (OSError, yaml.YAMLError) as error:
        return LintResult(
            message=f"{workflow_path.name}: failed to parse: {error}",
            is_issue=True,
            level=AnnotationLevel.ERROR,
        )
    if not isinstance(data, dict):
        return LintResult(
            message=f"{workflow_path.name}: invalid workflow structure.",
            is_issue=True,
            level=AnnotationLevel.ERROR,
        )
    return data


@cache
def canonical_caller_permissions(filename: str) -> dict[str, str]:
    """Union the job-level `permissions:` scopes of a canonical workflow.

    Memoized like {func}`extract_trigger_info`, and under the same read-only
    contract on the shared result.

    A caller job hands its own permissions down to the reusable workflow it
    calls, and the called workflow's jobs are capped by them: they cannot
    escalate beyond what the caller granted. The canonical workflows pin a
    top-level `permissions: {}`, so a job without its own block needs nothing
    and the union of the job-level blocks is the complete set the caller has
    to forward.

    A scope appearing at different levels across jobs resolves to the most
    permissive one, so no job is starved by another's narrower grant.

    :param filename: Canonical workflow filename (e.g., `autofix.yaml`).
    :return: Scope-to-level mapping, sorted by scope. Empty when no job
        declares permissions, meaning the caller forwards nothing.
    :raises FileNotFoundError: If the workflow file is not bundled.
    """
    data = yaml.safe_load(get_data_content(filename))
    union: dict[str, str] = {}
    for job in (data.get("jobs") or {}).values():
        if not isinstance(job, dict) or not isinstance(job.get("permissions"), dict):
            continue
        for raw_scope, raw_level in job["permissions"].items():
            scope, level = str(raw_scope), str(raw_level)
            # An unseen scope ranks below every level, so the first grant always
            # lands and later ones can only widen it.
            granted_rank = PERMISSION_RANK.get(union.get(scope, ""), -1)
            if PERMISSION_RANK.get(level, 0) > granted_rank:
                union[scope] = level
    return dict(sorted(union.items()))


@cache
def extract_trigger_info(filename: str) -> WorkflowTriggerInfo:
    """Extract trigger information from a bundled canonical workflow.

    Parses the workflow YAML and separates `workflow_call` configuration from
    other triggers.

    Memoized: the workflow lint asks for the same canonical's triggers once
    per check, per downstream file.

    ```{caution}
    The returned `WorkflowTriggerInfo` is shared between callers and holds
    mutable dicts: treat it as read-only, the way every reader does today.
    ```

    :param filename: Workflow filename (e.g., `release.yaml`).
    :return: Parsed trigger information.
    :raises FileNotFoundError: If the workflow file is not bundled.
    """
    content = get_data_content(filename)
    data = yaml.safe_load(content)

    name = data.get("name", filename)
    triggers = workflow_triggers(data)

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
      (upstream-specific files like `repomatic/data/labels.toml`).
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
    commit_sha: str | None = None,
    paths_spec: PathsSpec | None = None,
    with_permissions: bool = False,
    existing: str | None = None,
) -> str:
    """Generate a thin caller workflow for a reusable canonical workflow.

    The generated caller mirrors the canonical workflow's non-`workflow_call`
    triggers verbatim and delegates to the upstream workflow via `uses:`.
    `workflow_dispatch` is not injected: workflows that should expose manual
    dispatch declare it in the canonical definition. Declared `workflow_call`
    inputs and secrets are forwarded explicitly via `with:` and `secrets:`.

    Canonical `paths:` filters are adapted via *paths_spec* (see
    {class}`PathsSpec`).

    When *commit_sha* is provided, the `uses:` reference is SHA-pinned
    (`@sha # version`), secure-by-default from the first commit. The
    `sync-action-pins` job bumps it once a newer release clears the cooldown.

    :param filename: Canonical workflow filename (e.g., `release.yaml`).
    :param repo: Upstream repository (default: `kdeldycke/repomatic`).
    :param version: Version reference (default: `main`).
    :param commit_sha: Full 40-character commit SHA for the version tag.
        When provided, produces `@sha # version`. When `None`, produces
        `@version`.
    :param paths_spec: Full paths-adaptation spec; defaults to no adaptation.
    :param with_permissions: Emit an explicit permissions contract: a top-level
        `permissions: {}` plus, on the managed job, the scopes the reusable
        workflow needs (see {func}`canonical_caller_permissions`). Set when the
        downstream file carries extra jobs of its own, whose custom `steps:`
        are what make the top-level key worth pinning. Both halves ship
        together: the top-level `{}` alone would starve the managed call, which
        GitHub aborts at startup the moment a nested job asks for a scope the
        caller never granted.
    :param existing: Current content of the downstream file, when it already
        exists. Only `release.yaml` reads it, to carry over the extra `needs:`
        edges of its `release` lane; every other caller regenerates whole.
    :return: Complete YAML content for the thin caller workflow.
    :raises ValueError: If the workflow does not support `workflow_call`.
    """
    # release.yaml is a multi-job caller (build lane + publish-pypi + engine
    # lane), not a single thin delegation. Generate it by copying the canonical
    # caller and rewriting its local `uses:` refs. See _generate_release_caller.
    if filename == "release.yaml":
        return _generate_release_caller(
            repo=repo, version=version, commit_sha=commit_sha, existing=existing
        )

    spec = paths_spec if paths_spec is not None else PathsSpec()
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
    uses_ref = _pin_ref(version, commit_sha)
    main_job = filename.removesuffix(".yaml")
    lines = [
        "---",
        f"name: {info.name}",
        _render_triggers(triggers),
        "",
    ]

    if with_permissions:
        lines.extend(["permissions: {}", ""])

    lines.extend([
        "jobs:",
        "",
        f"  {main_job}:",
        f"    uses: {repo}/.github/workflows/{source}@{uses_ref}",
    ])

    # Restate the scopes the reusable workflow's own jobs declare. Under the
    # top-level `permissions: {}` above, this job would otherwise forward an
    # empty token and the called workflow could not escalate back out of it.
    if with_permissions:
        forwarded = canonical_caller_permissions(source)
        if forwarded:
            lines.append("    permissions:")
            lines.extend(
                f"      {scope}: {level}" for scope, level in forwarded.items()
            )
        else:
            # No job upstream asks for a scope, so forwarding nothing is the
            # accurate contract. Still spelled out, since an absent block would
            # read as an oversight rather than a deliberate empty grant.
            lines.append("    permissions: {}")

    # Forward workflow_call inputs, so a manual dispatch of the thin caller
    # reaches the reusable workflow. Canonical workflows only declare
    # workflow_call inputs as passthroughs of their own workflow_dispatch
    # inputs (enforced by tests/test_workflow_sync.py), which the caller
    # mirrors above, so `inputs.*` here resolves to the dispatch form values.
    # Boolean inputs are coerced with `== true`: on non-dispatch events
    # (schedule, push) the caller's `inputs` context is null, and GitHub does
    # not document how a null passed to a boolean-typed input behaves.
    if info.call_inputs:
        lines.append("    with:")
        for input_name, input_config in info.call_inputs.items():
            if (input_config or {}).get("type") == "boolean":
                input_value = f"${{{{ inputs.{input_name} == true }}}}"
            else:
                input_value = f"${{{{ inputs.{input_name} }}}}"
            lines.append(f"      {input_name}: {input_value}")

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


EXTRA_JOBS_SEPARATOR: Final[str] = "\n\n"
"""Gap between the last managed lane and the downstream extras below it.

Exactly one blank line, matching how {func}`_generate_release_caller` separates
its own jobs. Both sides are trimmed before it is applied, because neither end
is stable on its own: the release caller ends on a trailing blank line where a
plain thin caller does not, and {func}`extract_extra_jobs` slices from the end
of the last managed job body, so it returns however many blank lines the file
already had. Joining those two as-is added one blank line per sync, without
bound.
"""


def render_thin_caller_for_target(
    filename: str,
    target: Path,
    *,
    repo: str = DEFAULT_REPO,
    version: str = DEFAULT_VERSION,
    commit_sha: str | None = None,
    paths_spec: PathsSpec | None = None,
) -> tuple[str, str | None]:
    """Render the complete downstream content of *target*, extras included.

    The single seam between a canonical workflow and a file on disk: read what
    is already there, carry over what only the downstream copy knows, render the
    managed lanes, and re-attach the extras. `repomatic init` is the only
    caller, so a preservation argument can only ever be wired up once.

    ```{caution}
    Do not inline this back into a caller. It previously existed as two
    near-identical copies, and the `existing` argument that carries a consumer's
    `needs:` edges across a sync reached only one of them: every downstream
    `repomatic init` silently dropped the edge while the test suite, which drove
    the other copy, stayed green. `tests/test_workflow_sync.py` pins the seam to
    a single call site.
    ```

    Reads *target* itself rather than taking its content, so a caller cannot
    forget to hand over the state that preservation depends on.

    :param filename: Canonical workflow filename (e.g. `release.yaml`).
    :param target: Destination path, read when it already exists.
    :param repo: Upstream repository for the `uses:` refs.
    :param version: Version reference for the `uses:` refs.
    :param commit_sha: Full 40-character commit SHA for SHA-pinned refs.
    :param paths_spec: Full paths-adaptation spec; defaults to no adaptation.
    :return: The content to write, and the current content of *target* (`None`
        when it does not exist yet) so a caller can skip an unchanged write.
    :raises ValueError: If *filename* declares no `workflow_call` trigger.
    """
    existing = target.read_text(encoding="UTF-8") if target.exists() else None
    # Extra downstream jobs are read before generating, not after: their presence
    # is what decides whether the caller spells out a permissions contract. Those
    # jobs carry custom `steps:`, which is what makes a top-level
    # `permissions: {}` worth pinning.
    extra = extract_extra_jobs(existing, repo) if existing else ""

    content = generate_thin_caller(
        filename,
        repo,
        version,
        paths_spec=paths_spec,
        commit_sha=commit_sha,
        with_permissions=extras_define_jobs(extra),
        existing=existing,
    )
    if extra:
        content = content.rstrip("\n") + EXTRA_JOBS_SEPARATOR + extra.lstrip("\n")
    return content, existing


def _job_body_end(
    lines: Sequence[str], start: int, *, claim_indent_comments: bool
) -> int:
    """Index of the last line belonging to the job whose key sits at *start*.

    The one boundary rule for walking a job's raw body, shared by
    {func}`_extract_raw_job` and {func}`extract_extra_jobs`: both decide where
    a managed job ends in files `repomatic init` rewrites downstream, so two
    hand-kept copies of the rule risked duplicating or truncating job bodies
    the day they diverged. Anything indented deeper than the two-space job key
    is body whatever its exact depth (a hand-edited 3-space line must not end
    the walk early), a blank line rides along without extending the body, and
    a sibling key at the job indent or a dedent to column 0 ends it.

    The single deliberate divergence between the two callers is a comment at
    exactly the job indent. Extracting a bundled canonical job keeps it
    (*claim_indent_comments*), matching how those files are written; the
    extras split leaves it out, so a heading comment above a downstream job
    survives with that job rather than being swallowed by the managed one.

    :param lines: The workflow's lines.
    :param start: Index of the ``  {job}:`` key line.
    :param claim_indent_comments: Whether a job-indent comment counts as body.
    :return: Index of the body's last line; *start* itself for an empty body.
    """
    end = start
    for i in range(start + 1, len(lines)):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            continue
        indent = len(line) - len(line.lstrip())
        if indent > 2:
            end = i
            continue
        if indent == 2 and stripped.startswith("#") and claim_indent_comments:
            end = i
            continue
        break
    return end


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
    end = _job_body_end(lines, start, claim_indent_comments=True)
    return "\n".join(lines[start : end + 1])


_LOCAL_PUBLISH_PYPI_ACTION: Final[str] = "./.github/actions/publish-pypi"
"""Relative composite-action ref the canonical `release.yaml` dogfoods.

The canonical entry runs the in-tree action via this local ref (checking
itself out first). :func:`_render_publish_pypi_job` rewrites it to the pinned
cross-repo form (``{repo}/.github/actions/publish-pypi@{ref}``) for downstream
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
      of third-party action SHA pins, which `sync-action-pins` would not see
      (it scans `.yaml`, not this `.py`).
    - **Rewrite the local action ref** :data:`_LOCAL_PUBLISH_PYPI_ACTION` to the
      pinned cross-repo form so the caller's OIDC `job_workflow_ref` resolves to
      its own `release.yaml` (see pypi/warehouse#11096).

    The runner is carried through unchanged: downstream callers run on the same
    image repomatic uses. If a downstream repo turns out to need a fuller one,
    that surfaces as a failure to revisit then.

    Sourcing the body from a `.yaml` file (rather than building it in Python)
    keeps any future third-party SHA pin in the job visible to `sync-action-pins`.

    :param repo: Upstream repository owning the composite action.
    :param version: Version reference for the action ref.
    :param commit_sha: Optional 40-character SHA for pin-style refs (renders
        as `@sha # version`).
    :return: YAML lines (without trailing blank).
    :raises RuntimeError: If the canonical `release.yaml` no longer exposes a
        `publish-pypi` job shaped the way this renderer expects (missing job,
        `steps:` key, or the local action ref), meaning the entry changed in a
        way the renderer was not updated to handle.
    """
    action_ref = _pin_ref(version, commit_sha)
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
    ``uses: {repo}/.github/workflows/X.yaml@{uses_ref}``. All other lines pass
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


def _downstream_release_needs(existing: str | None, repo: str) -> tuple[str, ...]:
    """`needs:` edges the downstream `release` job adds to the canonical set.

    A consumer that builds its own release asset must gate the engine call on
    the job producing it, the same way the wheel gates on `build` (see the
    `release-assets` handoff in docs/workflows.md). That edge lives on a job
    this renderer regenerates, so it has to be carried across a sync or every
    `repomatic init` would silently drop it.

    Only edges pointing at downstream-defined jobs survive: an entry naming a
    managed lane is already in the canonical `needs:`, and one naming a job that
    no longer exists would abort the run at startup.

    :param existing: Current downstream `release.yaml`, or `None` when creating.
    :param repo: Upstream repository owning the managed lanes.
    :return: Extra job names, in the order the downstream file lists them.
    """
    if not existing:
        return ()
    try:
        data = yaml.safe_load(existing)
    except yaml.YAMLError:
        return ()
    if not isinstance(data, dict):
        return ()
    jobs = data.get("jobs")
    if not isinstance(jobs, dict):
        return ()
    release = jobs.get("release")
    if not isinstance(release, dict):
        return ()

    needs = release.get("needs")
    if isinstance(needs, str):
        needs = [needs]
    if not isinstance(needs, list):
        return ()

    upstream_pattern = _upstream_uses_pattern(repo)
    return tuple(
        name
        for name in needs
        if isinstance(name, str)
        and name in jobs
        and not _is_managed_job(jobs[name], upstream_pattern)
    )


GENERATED_CALLER_JOBS: Final[frozenset[str]] = frozenset({
    "build",
    "publish-pypi",
    "release",
})
"""Every job the generated downstream `release.yaml` defines.

The canonical entry may hold repomatic-local jobs beyond these three (its own
`pack-plugin`, for one), and only these three are copied downstream. Anything
the canonical `release` lane names in `needs:` outside this set has to be
dropped, or the generated file would reference a job that does not exist there.
See {func}`_merge_release_needs`.
"""


def _merge_release_needs(
    job_lines: list[str],
    extra: tuple[str, ...],
    known: frozenset[str] = GENERATED_CALLER_JOBS,
) -> list[str]:
    """Rewrite the release job's `needs:` for the generated downstream caller.

    Two filters, one in each direction. Canonical entries are kept only when
    *known* contains them, dropping an edge on a repomatic-local job that has no
    downstream counterpart. Then *extra* is appended, carrying the edges a
    consumer declared on its own asset-building job.

    Normalizes the canonical scalar (`needs: build`) into a block sequence so the
    entries stay one-per-line and diffable, and collapses back to the scalar form
    when only one survives, so the common case reads exactly as it did before.

    :param job_lines: The canonical `release` job, comment-stripped.
    :param extra: Downstream edges to add, from {func}`_downstream_release_needs`.
    :param known: Job names the generated file defines.
    :return: The job lines with `needs:` rewritten.
    """
    merged: list[str] = []
    index = 0
    while index < len(job_lines):
        line = job_lines[index]
        index += 1
        if not line.strip().startswith("needs:"):
            merged.append(line)
            continue

        indent = line[: len(line) - len(line.lstrip())]
        value = line.strip()[len("needs:") :].strip()
        entries = [value] if value else []
        # Absorb an existing block sequence before filtering and appending.
        while index < len(job_lines) and job_lines[index].strip().startswith("- "):
            entries.append(job_lines[index].strip()[2:].strip())
            index += 1
        entries = [name for name in entries if name in known]
        entries.extend(name for name in extra if name not in entries)

        if not entries:
            # Dropping the key beats emitting a bare `needs:`, which YAML
            # reads back as null and GitHub rejects at workflow startup.
            continue
        if len(entries) == 1:
            merged.append(f"{indent}needs: {entries[0]}")
            continue
        merged.append(f"{indent}needs:")
        merged.extend(f"{indent}  - {entry}" for entry in entries)

    return merged


def _generate_release_caller(
    repo: str,
    version: str,
    commit_sha: str | None,
    existing: str | None = None,
) -> str:
    """Generate the downstream `release.yaml` caller from the canonical entry.

    The canonical `release.yaml` (bundled, repomatic's own self-release entry) is
    a multi-job caller: a `build` lane call, the OIDC `publish-pypi` job, and a
    `release` engine call. Downstream repos get the same shape with:

    - **Standard release triggers synthesized** (`push` to `main` +
      `workflow_dispatch`), so a dogfooding-only trigger added upstream never
      leaks downstream. The canonical entry's `schedule` is the one trigger
      carried over: ordinary pushes only compile the `nuitka.dev-targets`
      canary, so the weekly run is what rebuilds every binary target (and
      keeps each target's compile cache warm) downstream too.
    - **Concurrency carried over** from the canonical entry verbatim, so
      superseded non-release runs cancel downstream too. The block must sit on
      this push-triggered caller (not the engine lane it calls via `needs:`,
      whose group joins too late to cancel queued runs); see docs/workflows.md.
    - **Deny-by-default `permissions: {}`** emitted like the canonical entry
      carries it, so a downstream job added below the managed lanes starts with
      no scopes instead of inheriting the repository default.
    - **Downstream `needs:` edges preserved** on the `release` lane (see
      :func:`_downstream_release_needs`), so a consumer gating the engine on its
      own asset-building job keeps that edge across a sync.
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
        `@sha # version`).
    :param existing: Current downstream `release.yaml`, read before generating so
        its extra `needs:` edges survive; `None` when creating the file.
    :return: Complete YAML content for the downstream `release.yaml` caller.
    :raises RuntimeError: If the canonical `release.yaml` no longer exposes the
        expected `build` and `release` jobs, meaning the entry changed in a way
        this renderer was not updated to handle.
    """
    content = get_data_content("release.yaml")
    info = extract_trigger_info("release.yaml")
    uses_ref = _pin_ref(version, commit_sha)

    triggers: dict[str, Any] = {
        "workflow_dispatch": None,
        "push": {"branches": ["main"]},
    }
    # Carry the canonical weekly full-fleet schedule downstream, copied from
    # the canonical entry rather than hard-coded so the batch time has one
    # source of truth. See the docstring above for why `schedule` is the one
    # trigger that crosses the synthesis boundary.
    schedule = info.non_call_triggers.get("schedule")
    if schedule:
        triggers["schedule"] = schedule

    build_job = _extract_raw_job(content, "build")
    release_job = _extract_raw_job(content, "release")
    if build_job is None or release_job is None:
        msg = (
            "Canonical release.yaml is missing its 'build' or 'release' job; the"
            " entry changed in a way _generate_release_caller was not updated to"
            " handle."
        )
        raise RuntimeError(msg)

    # Each lane is a `uses:`-only job: strip its repomatic-specific comments,
    # then pin the local `uses: ./` ref to the cross-repo form.
    def lane_lines(job_text: str) -> list[str]:
        return _rewrite_workflow_uses(
            _strip_comment_lines(job_text), repo, uses_ref
        ).split("\n")

    lines = [
        "---",
        f"name: {info.name}",
        _render_triggers(triggers),
        "",
        # Deny by default, mirroring the canonical entry: each lane below grants
        # itself the scopes it needs, and a downstream job appended after them
        # starts with none instead of the repository default.
        "permissions: {}",
        "",
    ]
    # Carry the entry workflow's concurrency group downstream verbatim (comments
    # included). It must live on this push-triggered caller, not the reusable
    # engine lane it reaches via `needs: build`, so superseded non-release runs
    # cancel while release commits keep their unique SHA group. See the canonical
    # release.yaml and docs/workflows.md.
    if info.raw_concurrency:
        lines.append(info.raw_concurrency)
        lines.append("")
    # The publish-pypi job below runs on the downstream runner, so it needs its
    # own cooldown: a workflow-level `env:` does not cross into the reusable
    # lanes this caller invokes, nor back out of them.
    lines.append(cooldown_env_block())
    lines.extend([
        "jobs:",
        "",
        *lane_lines(build_job),
    ])
    lines.extend(
        _render_publish_pypi_job(repo=repo, version=version, commit_sha=commit_sha)
    )
    lines.append("")
    lines.extend(
        _merge_release_needs(
            lane_lines(release_job), _downstream_release_needs(existing, repo)
        )
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


def _upstream_uses_pattern(repo: str) -> re.Pattern[str]:
    """Match a `uses:` reference pointing at *repo*'s workflows or actions."""
    return re.compile(rf"{re.escape(repo)}/\.github/(workflows|actions)/")


def _is_managed_job(config: Any, upstream_pattern: re.Pattern[str]) -> bool:
    """Whether a job body is one repomatic owns.

    A job is managed when it references the upstream repository, either at the
    job-level `uses:` (reusable workflow) or at any `steps[*].uses:` (composite
    action like `kdeldycke/repomatic/.github/actions/publish-pypi`). Everything
    else was written downstream.
    """
    if not isinstance(config, dict):
        return False
    if upstream_pattern.search(config.get("uses", "")):
        return True
    steps = config.get("steps") or []
    return isinstance(steps, list) and any(
        isinstance(step, dict) and upstream_pattern.search(step.get("uses", ""))
        for step in steps
    )


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

    upstream_pattern = _upstream_uses_pattern(repo)
    managed_keys = [
        str(key)
        for key, config in jobs.items()
        if _is_managed_job(config, upstream_pattern)
    ]
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
        # The shared boundary rule; see _job_body_end for why comments at the
        # job indent stay outside the body on this side of the split.
        last_body_idx = _job_body_end(
            all_lines, managed_idx, claim_indent_comments=False
        )

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


def extras_define_jobs(extra: str) -> bool:
    """Whether an extras fragment holds actual job definitions.

    A fragment can be comments and blank lines only (a trailing note kept
    after the managed lanes): that content is worth carrying over verbatim,
    but it must not flip the caller into the explicit-permissions contract
    reserved for real downstream jobs.
    """
    return any(
        line.strip() and not line.strip().startswith("#") for line in extra.split("\n")
    )


# ---------------------------------------------------------------------------
# Lint checks
# ---------------------------------------------------------------------------


def check_has_workflow_dispatch(workflow_path: Path) -> LintResult:
    """Check that a workflow has a `workflow_dispatch` trigger.

    :param workflow_path: Path to the workflow file.
    :return: Lint result.
    """
    data = _parse_workflow(workflow_path)
    if isinstance(data, LintResult):
        return data

    if "workflow_dispatch" not in workflow_triggers(data):
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
    data = _parse_workflow(workflow_path)
    if isinstance(data, LintResult):
        return data

    caller_triggers = set(workflow_triggers(data))
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

    data = _parse_workflow(workflow_path)
    if isinstance(data, LintResult):
        return data

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
    paths_spec: PathsSpec | None = None,
) -> str:
    """Return the raw header of a canonical workflow.

    The header is everything before the `jobs:` line: `name`, `on`
    triggers, `concurrency`, and any comments.

    Each `paths:` block in the header is rewritten using *paths_spec*:
    upstream source references substituted, optional extras appended,
    ignored entries stripped, or replaced wholesale via a per-workflow
    override (see {class}`PathsSpec`). When the resulting
    list is empty, the entire `paths:` block is removed. Comments outside
    the rewritten blocks are preserved verbatim; comments inside an
    entry block are not supported.

    :param filename: Canonical workflow filename (e.g., `tests.yaml`).
    :param paths_spec: Full paths-adaptation spec; defaults to no adaptation.
    :return: Raw header text.
    :raises FileNotFoundError: If the workflow file is not bundled.
    :raises ValueError: If no `jobs:` line is found.
    """
    spec = paths_spec if paths_spec is not None else PathsSpec()
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
            # Style is a preference, validity is not. A block whose entries are
            # all plain yields an empty canonical quote, which silently breaks
            # any override needing one: `**/*.py` emitted bare reads as an
            # alias and the workflow stops parsing, so GitHub ignores the file
            # outright. Fall back to a double quote whenever the inherited
            # style cannot carry the value.
            if not quote and _needs_yaml_quote(entry):
                quote = '"'
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


def _needs_yaml_quote(value: str) -> bool:
    """Whether *value* survives as a plain scalar in a block sequence entry.

    Asks the parser instead of testing against a table of YAML indicators,
    which keeps the two rule sets from drifting and covers the coercions an
    indicator table misses: a path spelled `yes`, `null` or `1.0` parses back
    as a non-string and so needs quoting just as much as one opening with `*`.

    :param value: Unquoted entry text.
    :return: `True` when the entry has to be quoted to round-trip.
    """
    try:
        return bool(yaml.safe_load(f"- {value}\n") != [value])
    except yaml.YAMLError:
        # Unparsable as a plain scalar is the strongest possible case for
        # quoting: `*` opens an alias, `&` an anchor, `!` a tag.
        return True


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
