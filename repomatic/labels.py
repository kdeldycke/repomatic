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

The label domain in one place: matching an issue or pull request against the
structured `[tool.repomatic.labels]` rules to decide which labels it earns,
and applying label definitions to a repository through `labelmaker`. Backs the
`apply-labels` and `sync-labels` commands.

```{note}
The rule schema is the one `actions/labeler` and `github/issue-labeler`
defined, kept verbatim after those two actions were retired: the matcher
below reimplements their semantics rather than exporting to them, so a
downstream `[tool.repomatic.labels]` written against the actions keeps
working untouched.

One semantic deliberately did not survive the port. `github/issue-labeler`
AND-joined a label's patterns, which made the obvious "any of these keywords"
rule silently dead, and cost this module a warning telling authors to collapse
their list into one alternation. Patterns are OR-joined here, so that rule now
does what it reads like.
```
"""

from __future__ import annotations

import logging
import re
import subprocess
import tempfile
from pathlib import Path

import tomlrt
import yaml
from wcmatch import glob

from .bundle import get_data_content
from .tool_runner import ensure_binary

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from typing import Any

    from .config import Config

FILE_RULE_CHANGED_FILES_MATCHERS: tuple[str, ...] = (
    "any-glob-to-any-file",
    "any-glob-to-all-files",
    "all-globs-to-any-file",
    "all-globs-to-all-files",
)
"""Matchers nested under a group's `changed-files` key."""

FILE_RULE_BRANCH_MATCHERS: tuple[str, ...] = ("head-branch", "base-branch")
"""Matchers that sit at the same level as `changed-files`."""

FILE_RULE_GROUP_WRAPPERS: tuple[str, ...] = ("any", "all")
"""Group wrappers whose value is a list of nested sub-groups."""

FILE_RULE_MATCHER_KEYS: frozenset[str] = frozenset((
    *FILE_RULE_CHANGED_FILES_MATCHERS,
    *FILE_RULE_BRANCH_MATCHERS,
    *FILE_RULE_GROUP_WRAPPERS,
))
"""Keys valid inside a match group (top-level entry minus `label`, or nested)."""

CONTENT_RULE_KNOWN_KEYS: frozenset[str] = frozenset(("label", "patterns"))
"""All keys recognized on a single `[[labels.content-rules]]` TOML entry."""

CONTENT_PATTERN_RE = re.compile(r"^/(?P<body>.*)/(?P<flags>[a-z]*)$", re.DOTALL)
"""The `/body/flags` spelling a content pattern may take, mirroring JavaScript.

A pattern not in this shape is used as the regex itself, case-sensitively,
which is what `github/issue-labeler` did with a bare entry. Authors reach for
the slashed form to get `i`, since users capitalize freely.
"""

CONTENT_PATTERN_FLAGS: dict[str, int] = {
    "i": re.IGNORECASE,
    "m": re.MULTILINE,
    "s": re.DOTALL,
}
"""JavaScript regex flags with a Python equivalent, and their translation.

The rest of JavaScript's set is accepted and ignored rather than rejected: `g`
and `y` govern stateful iteration that a single membership test never reaches,
`u` and `v` describe a Unicode mode Python's `re` is always in, and `d` only
adds capture-group offsets nobody here reads. Refusing them would fail a rule
over a flag that changes nothing about whether it matches.
"""

GLOB_FLAGS = glob.GLOBSTAR | glob.DOTGLOB | glob.BRACE | glob.NEGATE | glob.NEGATEALL
"""`wcmatch` flags reproducing the `minimatch` dialect `actions/labeler` used.

`GLOBSTAR` gives `**` its cross-directory meaning, `BRACE` expands `{a,b}`, and
`NEGATE` honours a leading `!`. The two worth spelling out:

- `DOTGLOB`, because `actions/labeler` passed `{dot: true}` and half the globs
  a repository cares about start with a dot (`.github/**/*`). Without it a
  workflow change matches nothing.
- `NEGATEALL`, because `minimatch` reads a lone `!**/*.md` as "everything that
  is not markdown", while `wcmatch` defaults to matching nothing at all
  when no positive pattern accompanies the exclusion.
"""

INLINE_LABEL_FIELDS: tuple[str, ...] = (
    "name",
    "color",
    "description",
    "create",
    "update",
    "enforce-case",
    "rename-from",
    "on-rename-clash",
)
"""Per-label fields of labelmaker's specification, in its documented order.

`serialize_inline_labels` passes them through verbatim (colors get their
leading `#` stripped), so declarative renames and the other per-label knobs
ride the regular sync.

`rename-from` is the one field with a constraint worth knowing before use: it
is strictly one-to-one, renaming only when the target is absent and exactly one
listed source exists. It therefore cannot merge several labels into one, and is
useless once a sync has already created the target. See "Retiring a label is a
migration, not a deletion" in `claude.md`."""


def _load_bundled_rules(filename: str) -> dict[str, list[Any]]:
    """Read one of the two bundled default rule files.

    These ship inside the package rather than being written into the
    repository: nothing outside this module reads them any more, so a copy on
    disk would only be a second version of the truth to keep in step.
    """
    loaded = yaml.safe_load(get_data_content(filename)) or {}
    return {label: list(rules) for label, rules in loaded.items()}


def _render_file_rule_group(group: dict[str, Any], label: str) -> dict[str, Any]:
    """Normalize one TOML match group into the shape the matcher walks.

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


def load_file_rules(config: Config | None = None) -> dict[str, list[dict[str, Any]]]:
    """Merge the bundled default file rules with the project's own.

    Each `[[tool.repomatic.labels.file-rules]]` entry becomes one match group
    under its `label`, appended after whatever the bundled defaults already
    declare for that label. Groups under one label are OR'd, so a project adds
    to a default rule rather than replacing it. Entries without a `label` or
    without any matcher are skipped with a warning: the first cannot be
    attributed, and the second would label every pull request.

    Keep the globs precise: a file rule should match paths owned by exactly one
    label. A glob broad enough to catch unrelated changes mislabels every PR
    touching them, and the labeller is only a convenience for the maintainer's
    first pass, never a substitute for manual classification (see `claude.md`).

    :param config: The resolved `[tool.repomatic]` configuration, or `None` for
        the bundled defaults alone.
    :return: Match groups keyed by label.
    """
    grouped = _load_bundled_rules("labeller-file-based.yaml")
    for rule in config.labels.file_rules if config else ():
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
    return grouped


def load_content_rules(config: Config | None = None) -> dict[str, list[str]]:
    """Merge the bundled default content rules with the project's own.

    Each `[[tool.repomatic.labels.content-rules]]` entry contributes its
    `patterns` to its `label`, after whatever the bundled defaults declare.
    Repeating a label across entries concatenates, and every pattern under a
    label is OR'd at match time, so adding a keyword never disables the ones
    already there. Entries without a `label` or without `patterns` are skipped
    with a warning.

    Encode a rule only for terms that unambiguously name the subject and that
    the project never prints in its own output; see the labeller principle in
    `claude.md`.

    :param config: The resolved `[tool.repomatic]` configuration, or `None` for
        the bundled defaults alone.
    :return: Patterns keyed by label.
    """
    grouped = _load_bundled_rules("labeller-content-based.yaml")
    for rule in config.labels.content_rules if config else ():
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
    return grouped


def compile_content_pattern(pattern: str) -> re.Pattern[str] | None:
    """Compile one content pattern, in either the bare or the `/body/flags` form.

    Returns `None` on a pattern the `re` module rejects, having logged it. A
    single malformed rule must not take the whole labelling run down with it:
    the job is a convenience that runs once per opened issue, and the other
    rules still have work to do.
    """
    flags = 0
    if slashed := CONTENT_PATTERN_RE.match(pattern):
        body = slashed["body"]
        for flag in slashed["flags"]:
            flags |= CONTENT_PATTERN_FLAGS.get(flag, 0)
    else:
        body = pattern
    try:
        return re.compile(body, flags)
    except re.error as error:
        logging.warning("Skipping malformed content pattern %r: %s.", pattern, error)
        return None


def match_content_rules(rules: dict[str, list[str]], text: str) -> set[str]:
    """Return every label whose patterns match *text*.

    :param rules: Patterns keyed by label, from {func}`load_content_rules`.
    :param text: The issue or pull request title and body, concatenated.
    :return: The matching labels.
    """
    matched = set()
    for label, patterns in rules.items():
        for pattern in patterns:
            compiled = compile_content_pattern(pattern)
            if compiled is not None and compiled.search(text):
                logging.debug(f"Content pattern {pattern!r} matched {label!r}.")
                matched.add(label)
                break
    return matched


def _match_changed_files(
    matcher: str,
    globs: Sequence[str],
    files: Sequence[str],
) -> bool:
    """Evaluate one `changed-files` matcher over the changed file list.

    The four matcher names are the two quantifiers crossed: whether *any* or
    *all* globs must hit, and whether *any* or *all* files must be hit.

    A pull request with no changed files never matches, whichever matcher is
    named. Three of the four are universally quantified over files, so the
    empty case would otherwise be vacuously true and label an empty diff with
    everything.
    """
    if not files or not globs:
        return False
    hits = {
        (file, pattern): glob.globmatch(file, pattern, flags=GLOB_FLAGS)
        for file in files
        for pattern in globs
    }
    if matcher == "any-glob-to-any-file":
        return any(hits.values())
    if matcher == "any-glob-to-all-files":
        return all(any(hits[f, p] for p in globs) for f in files)
    if matcher == "all-globs-to-any-file":
        return any(all(hits[f, p] for p in globs) for f in files)
    if matcher == "all-globs-to-all-files":
        return all(hits.values())
    logging.warning("Unknown changed-files matcher %r (ignored).", matcher)
    return False


def _match_branch(patterns: Iterable[str], branch: str) -> bool:
    """Whether *branch* matches any of the unanchored regex *patterns*."""
    if not branch:
        return False
    return any(
        compiled.search(branch)
        for compiled in (compile_content_pattern(p) for p in patterns)
        if compiled is not None
    )


def _match_file_rule_group(
    group: dict[str, Any],
    files: Sequence[str],
    head_branch: str,
    base_branch: str,
) -> bool:
    """Evaluate one match group, AND-ing every condition it carries.

    An empty group returns `False` rather than the vacuous `True` an
    unqualified `all()` would give: a group with nothing to test cannot have
    been meant to match everything.
    """
    conditions: list[bool] = []
    for matcher_entry in group.get("changed-files", ()):
        for matcher, globs in matcher_entry.items():
            conditions.append(_match_changed_files(matcher, globs, files))
    if "head-branch" in group:
        conditions.append(_match_branch(group["head-branch"], head_branch))
    if "base-branch" in group:
        conditions.append(_match_branch(group["base-branch"], base_branch))
    if "any" in group:
        conditions.append(
            any(
                _match_file_rule_group(sub, files, head_branch, base_branch)
                for sub in group["any"]
            )
        )
    if "all" in group:
        conditions.append(
            all(
                _match_file_rule_group(sub, files, head_branch, base_branch)
                for sub in group["all"]
            )
        )
    return bool(conditions) and all(conditions)


def match_file_rules(
    rules: dict[str, list[dict[str, Any]]],
    files: Sequence[str],
    head_branch: str = "",
    base_branch: str = "",
) -> set[str]:
    """Return every label whose match groups accept this pull request.

    :param rules: Match groups keyed by label, from {func}`load_file_rules`.
    :param files: Paths the pull request changes, relative to the repo root.
    :param head_branch: The pull request's head branch name.
    :param base_branch: The pull request's base branch name.
    :return: The matching labels.
    """
    matched = set()
    for label, groups in rules.items():
        for group in groups:
            if _match_file_rule_group(group, files, head_branch, base_branch):
                logging.debug(f"File rule group {group!r} matched {label!r}.")
                matched.add(label)
                break
    return matched


def serialize_inline_labels(entries: list[dict[str, Any]]) -> str:
    """Serialize `[tool.repomatic.labels.extra]` entries to a labelmaker TOML config.

    Each entry becomes a `[[profiles.default.labels]]` block under the `default`
    profile, carrying every per-label field of labelmaker's specification
    (`INLINE_LABEL_FIELDS`): a `rename-from` list renames a label in place on
    GitHub, preserving its issue and PR associations, and the `create`,
    `update`, `enforce-case` and `on-rename-clash` knobs pass through alike.
    Leading `#` on hex colors is stripped, on both single colors and multi-color
    lists, so the output matches labelmaker's convention.

    Entries missing a `name` are skipped with a warning, and unknown fields are
    dropped with a warning: labelmaker rejects both and would abort the whole
    sync.

    Returns an empty string when there are no valid entries, so the caller can
    skip writing a temp file and invoking labelmaker entirely.
    """
    labels: list[dict[str, Any]] = []
    for entry in entries:
        name = str(entry.get("name", "")).strip()
        if not name:
            logging.warning(
                "Skipping inline label without a `name`: %r.",
                entry,
            )
            continue
        label: dict[str, Any] = {"name": name}
        for field_id in INLINE_LABEL_FIELDS[1:]:
            if field_id not in entry:
                continue
            value = entry[field_id]
            # Emptied fields are omitted, never emitted as blanks. Booleans
            # pass: an explicit `create = false` is meaningful.
            if value is None or value == "" or value == []:
                continue
            if field_id == "color":
                if isinstance(value, list):
                    value = [str(color).lstrip("#") for color in value]
                else:
                    value = str(value).lstrip("#")
            label[field_id] = value
        if unknown := sorted(set(entry) - set(INLINE_LABEL_FIELDS)):
            logging.warning(
                "Ignoring unknown fields %s on inline label %r.",
                ", ".join(map(repr, unknown)),
                name,
            )
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


def apply_labels(
    config: Config,
    repository: str,
    *,
    is_awesome: bool,
    labels_dir: Path | None = None,
) -> None:
    """Apply every configured label source to *repository* via `labelmaker`.

    Applies, in order: the exported `labels.toml` under the `default` profile,
    the `awesome` profile for `awesome-*` repositories, any hand-written or
    downloaded files under `extra-labels/`, and the inline
    `[tool.repomatic.labels.extra]` definitions. The exported files are
    expected to exist already (written by {func}`~repomatic.init_project.run_init`
    for the `labels` component).

    :param config: The resolved `[tool.repomatic]` configuration.
    :param repository: GitHub repository in `owner/name` form.
    :param is_awesome: Whether the repository is an `awesome-*` list.
    :param labels_dir: Directory holding the exported `labels.toml` and the
        `extra-labels/` downloads. Defaults to the current directory. Point it
        at a scratch directory to keep the export out of the working tree.
    :raises RuntimeError: When a labelmaker invocation fails.
    """
    base = Path() if labels_dir is None else labels_dir
    labels_toml = str(base / "labels.toml")

    # Hand-written files committed under `extra-labels/` in the repository,
    # plus any downloaded from `labels.extra-files` into the export directory.
    # Keyed by filename so a download shadows a committed file of the same
    # name, which is what happened when both landed in the same directory.
    extra_files: dict[str, Path] = {}
    for extra_dir in (Path("extra-labels"), base / "extra-labels"):
        if not extra_dir.is_dir():
            continue
        for label_file in sorted(extra_dir.iterdir()):
            if label_file.is_file():
                extra_files[label_file.name] = label_file

    lm = ensure_binary("labelmaker")

    # Apply default profile.
    _run_labelmaker(lm, "apply", labels_toml, "--profile", "default", repository)

    # Apply awesome profile for awesome-* repos.
    if is_awesome:
        _run_labelmaker(lm, "apply", labels_toml, "--profile", "awesome", repository)

    # Apply extra label files.
    for _name, label_file in sorted(extra_files.items()):
        _run_labelmaker(lm, "apply", str(label_file), repository)

    # Apply inline label definitions from `[tool.repomatic.labels.extra]`.
    inline_toml = serialize_inline_labels(config.labels.extra)
    if inline_toml:
        with tempfile.TemporaryDirectory(prefix="repomatic-labels-") as tmpdir:
            inline_file = Path(tmpdir) / "inline.toml"
            inline_file.write_text(inline_toml, encoding="UTF-8")
            _run_labelmaker(lm, "apply", str(inline_file), repository)
