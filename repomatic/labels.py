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

"""Repository label management.

The label domain in one place: rendering the structured
`[tool.repomatic.labels]` rules into the labeller YAML dialects at export
time, and applying label definitions to a repository through `labelmaker` at
sync time. Backs the `sync-labels` command and the label-file export of
`repomatic init labels`.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path

import tomlrt
import yaml

from .tool_runner import binary_tool_context

TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Any

    from .config import Config

FILE_RULE_CHANGED_FILES_MATCHERS: tuple[str, ...] = (
    "any-glob-to-any-file",
    "any-glob-to-all-files",
    "all-globs-to-any-file",
    "all-globs-to-all-files",
)
"""`actions/labeler` matchers nested under `changed-files` in the rendered YAML."""

FILE_RULE_BRANCH_MATCHERS: tuple[str, ...] = ("head-branch", "base-branch")
"""`actions/labeler` matchers that sit at the same level as `changed-files`."""

FILE_RULE_GROUP_WRAPPERS: tuple[str, ...] = ("any", "all")
"""`actions/labeler` group wrappers whose value is a list of nested sub-groups."""

FILE_RULE_MATCHER_KEYS: frozenset[str] = frozenset((
    *FILE_RULE_CHANGED_FILES_MATCHERS,
    *FILE_RULE_BRANCH_MATCHERS,
    *FILE_RULE_GROUP_WRAPPERS,
))
"""Keys valid inside a match group (top-level entry minus `label`, or nested)."""

CONTENT_RULE_KNOWN_KEYS: frozenset[str] = frozenset(("label", "patterns"))
"""All keys recognized on a single `[[labels.content-rules]]` TOML entry."""


def _dump_labeller_yaml(grouped: dict[str, Any]) -> str:
    """Serialize a label-keyed dict to the labeller YAML dialect.

    Both `actions/labeler` (file rules) and `github/issue-labeler` (content
    rules) consume top-level `label: ...` mappings; the only shared dump
    settings are `default_flow_style=False`, `sort_keys=False`, and
    `allow_unicode=True` (labels contain emojis). `width=10000` keeps long
    glob lists on one line for readable diffs. Returns an empty string when
    `grouped` is empty so the caller can skip the labeller append entirely.
    """
    if not grouped:
        return ""
    return yaml.safe_dump(
        grouped,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
        width=10000,
    )


def _render_file_rule_group(group: dict[str, Any], label: str) -> dict[str, Any]:
    """Render one TOML match group as an `actions/labeler` YAML group dict.

    File-level matchers (`any-glob-to-any-file` etc.) collapse under a single
    `changed-files` key. Branch matchers pass through at the group's top level.
    The `any` / `all` group wrappers recurse into nested groups, so a downstream
    project can express the full `actions/labeler` v5+ schema without falling
    back to raw YAML. Unknown keys log a warning and are dropped.
    """
    rendered: dict[str, Any] = {}
    for key in group:
        if key not in FILE_RULE_MATCHER_KEYS:
            logging.warning(
                "Unknown file rule key %r in label %r (ignored).",
                key,
                label,
            )
    changed_files: list[dict[str, list[str]]] = []
    for key in FILE_RULE_CHANGED_FILES_MATCHERS:
        value = group.get(key)
        if value:
            changed_files.append({key: list(value)})
    if changed_files:
        rendered["changed-files"] = changed_files
    for key in FILE_RULE_BRANCH_MATCHERS:
        value = group.get(key)
        if value:
            rendered[key] = list(value)
    for wrapper in FILE_RULE_GROUP_WRAPPERS:
        value = group.get(wrapper)
        if not value:
            continue
        sub_groups = []
        for sub in value:
            sub_rendered = _render_file_rule_group(sub, label)
            if sub_rendered:
                sub_groups.append(sub_rendered)
            else:
                logging.warning(
                    "Skipping empty %r sub-group for label %r.",
                    wrapper,
                    label,
                )
        if sub_groups:
            rendered[wrapper] = sub_groups
    return rendered


def serialize_file_rules(rules: list[dict[str, Any]]) -> str:
    """Serialize structured file-rules to `actions/labeler` YAML.

    Each rule entry becomes one match group under its `label`. Repeating the
    same `label` across entries OR's the resulting groups, matching the
    labeller convention. Entries without a `label` or without any matcher are
    skipped with a warning, since the labeller would either crash or apply
    the label to every PR.

    Keep the globs precise: a file rule should match paths owned by exactly one
    label. A glob broad enough to catch unrelated changes mislabels every PR
    touching them, and the labeller is only a convenience for the maintainer's
    first pass, never a substitute for manual classification (see `claude.md`).
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for rule in rules:
        label = str(rule.get("label") or "").strip()
        if not label:
            logging.warning("Skipping file rule without `label`: %r.", rule)
            continue
        group_body = {k: v for k, v in rule.items() if k != "label"}
        group = _render_file_rule_group(group_body, label)
        if not group:
            logging.warning(
                "Skipping file rule for label %r with no matchers: %r.",
                label,
                rule,
            )
            continue
        grouped.setdefault(label, []).append(group)
    return _dump_labeller_yaml(grouped)


def serialize_content_rules(rules: list[dict[str, Any]]) -> str:
    r"""Serialize structured content-rules to `github/issue-labeler` YAML.

    Each rule entry contributes its `patterns` list under its `label`.
    Repeating the same `label` across entries concatenates the patterns.
    Entries without a `label` or without `patterns` are skipped with a
    warning.

    `github/issue-labeler` AND-joins a label's patterns (every one must match)
    and matches each as an unanchored, case-sensitive regex. A downstream rule
    that means "any of these keywords" must therefore supply a *single* anchored,
    case-insensitive alternation (`/\bfoo\b|\bbar\b/i`), not a list of bare words.
    Encode a rule only for terms that unambiguously name the subject and that the
    project never prints in its own output; see the labeller principle in
    `claude.md`.
    """
    grouped: dict[str, list[str]] = {}
    for rule in rules:
        label = str(rule.get("label") or "").strip()
        if not label:
            logging.warning("Skipping content rule without `label`: %r.", rule)
            continue
        for key in rule:
            if key not in CONTENT_RULE_KNOWN_KEYS:
                logging.warning(
                    "Unknown content rule key %r in label %r (ignored).",
                    key,
                    label,
                )
        patterns = rule.get("patterns") or []
        if not patterns:
            logging.warning(
                "Skipping content rule for label %r with no patterns: %r.",
                label,
                rule,
            )
            continue
        grouped.setdefault(label, []).extend(patterns)
    return _dump_labeller_yaml(grouped)


def augment_labeller_content(
    source: str,
    content: str,
    config: Config | None,
) -> str:
    """Append structured per-label rules to a bundled labeller YAML.

    The bundled YAML is the canonical default rules upstream ships. Downstream
    projects add their own rules as structured TOML under
    `[[tool.repomatic.labels.file-rules]]` or
    `[[tool.repomatic.labels.content-rules]]`; the structured form is rendered
    to YAML at export time and concatenated after the bundled content with a
    blank-line separator. Pass-through for non-labeller source files.
    """
    if config is None:
        return content
    if source == "labeller-file-based.yaml":
        structured = serialize_file_rules(config.labels.file_rules)
    elif source == "labeller-content-based.yaml":
        structured = serialize_content_rules(config.labels.content_rules)
    else:
        return content
    if not structured.strip():
        return content
    return content.rstrip() + "\n\n" + structured.rstrip() + "\n"


def serialize_inline_labels(entries: list[dict[str, str]]) -> str:
    """Serialize `[tool.repomatic.labels.extra]` entries to a labelmaker TOML config.

    Each entry becomes a `[[profiles.default.labels]]` block under the `default`
    profile. Leading `#` on hex colors is stripped so the output matches
    labelmaker's convention. Entries missing a `name` are skipped with a warning:
    labelmaker rejects nameless labels and would abort the whole sync.

    Returns an empty string when there are no valid entries, so the caller can
    skip writing a temp file and invoking labelmaker entirely.
    """
    labels: list[dict[str, str]] = []
    for entry in entries:
        name = entry.get("name", "").strip()
        if not name:
            logging.warning(
                "Skipping inline label without a `name`: %r.",
                entry,
            )
            continue
        label: dict[str, str] = {"name": name}
        if color := entry.get("color"):
            label["color"] = color.lstrip("#")
        if description := entry.get("description"):
            label["description"] = description
        labels.append(label)

    if not labels:
        return ""

    doc = tomlrt.Document({"profiles": {"default": {"labels": labels}}})
    return tomlrt.dumps(doc)


def _run_labelmaker(labelmaker_path: Path, *args: str) -> None:
    """Run a `labelmaker` command.

    :param labelmaker_path: Path to the labelmaker binary.
    :param args: Arguments to pass to labelmaker.
    :raises RuntimeError: If labelmaker fails.
    """
    cmd = [str(labelmaker_path), *args]
    logging.info(f"Running: {' '.join(cmd)}")
    result = subprocess.run(
        cmd,
        capture_output=True,
        encoding="UTF-8",
        check=False,
    )
    if result.returncode:
        msg = f"labelmaker failed: {result.stderr}"
        raise RuntimeError(msg)
    if result.stdout:
        logging.debug(result.stdout)


def apply_labels(config: Config, repository: str, *, is_awesome: bool) -> None:
    """Apply every configured label source to *repository* via `labelmaker`.

    Applies, in order: the exported `labels.toml` under the `default` profile,
    the `awesome` profile for `awesome-*` repositories, any hand-written or
    downloaded files under `extra-labels/`, and the inline
    `[tool.repomatic.labels.extra]` definitions. The label files are expected
    on disk already (exported by `repomatic init labels`).

    :param config: The resolved `[tool.repomatic]` configuration.
    :param repository: GitHub repository in `owner/name` form.
    :param is_awesome: Whether the repository is an `awesome-*` list.
    :raises RuntimeError: When a labelmaker invocation fails.
    """
    with binary_tool_context("labelmaker") as lm:
        # Apply default profile.
        _run_labelmaker(lm, "apply", "labels.toml", "--profile", "default", repository)

        # Apply awesome profile for awesome-* repos.
        if is_awesome:
            _run_labelmaker(
                lm, "apply", "labels.toml", "--profile", "awesome", repository
            )

        # Apply extra label files.
        extra_dir = Path("extra-labels")
        if extra_dir.is_dir():
            for label_file in sorted(extra_dir.iterdir()):
                if label_file.is_file():
                    _run_labelmaker(lm, "apply", str(label_file), repository)

        # Apply inline label definitions from `[tool.repomatic.labels.extra]`.
        inline_toml = serialize_inline_labels(config.labels.extra)
        if inline_toml:
            with tempfile.TemporaryDirectory(prefix="repomatic-labels-") as tmpdir:
                inline_file = Path(tmpdir) / "inline.toml"
                inline_file.write_text(inline_toml, encoding="UTF-8")
                _run_labelmaker(lm, "apply", str(inline_file), repository)
