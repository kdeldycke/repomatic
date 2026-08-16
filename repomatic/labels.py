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
`[tool.repomatic.labels]` rules to decide which labels it earns, and applying
label definitions to a repository through `labelmaker`. Backs the
`apply-labels` and `sync-labels` commands.

A rule is one label mapped to a list of patterns: regexes or keywords over the
thread's text ({data}`DEFAULT_CONTENT_RULES`), globs over a pull request's
changed paths ({data}`DEFAULT_FILE_RULES`). Any pattern matching applies the
label. A project entry for a label replaces the default entry wholesale, and
an empty list disables it; see {func}`resolve_content_rules`.

```{note}
This schema replaced the `actions/labeler` v5 and `github/issue-labeler`
dialects when the matching moved in-tree. The retired shapes earned their
complexity serving the actions (per-matcher quantifiers, branch regexes,
`any`/`all` group nesting, per-pattern AND-joins), and none of it was used by
any repository this toolkit manages: every real rule was "label X when any
changed file matches any of these globs" or "when any of these words appears",
which is exactly what the schema now says and nothing more.
```
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
from functools import cache
from pathlib import Path

import tomlrt
from wcmatch import glob

from .github.gh import resolve_gh_token
from .tool_runner import ensure_binary

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from typing import Any

    from .config import Config

DEFAULT_CONTENT_RULES: dict[str, tuple[str, ...]] = {
    "🐛 bug": ("bug", "error", "exception", "fix", "traceback"),
    "🆙 changelog": ("change-log", "changelog"),
    "🤖 ci": (
        ".github",
        "actions",
        "ci-cd",
        "cicd",
        "coverage",
        "gitignore",
        "workflow",
    ),
    "🔗 dependencies": (".lock", "pyproject.toml"),
    "📚 documentation": (
        "docstring",
        "license",
        "mailmap",
        "markdown",
        "readme",
        "sphinx",
        "typo",
    ),
}
"""Default content rules: keywords matched against a thread's title and body.

Every entry is a plain keyword, compiled case-insensitively with word
boundaries on its word-character edges (see {func}`compile_content_pattern`),
so `Bug` matches and `prefix` does not trip `fix`. The keys must name labels
that `repomatic/data/labels.toml` defines, or the labelling call fails on a
label GitHub does not have; `tests/test_labels.py` enforces that.

Tune for precision, not recall: a missing label costs one manual click, a
wrong one is noise on every issue that trips it. Never key a rule off a token
the project prints in its own output, or a user pasting a trace sets every
label at once.

```{note}
`💖 sponsor` deliberately has no rule here, nor in {data}`DEFAULT_FILE_RULES`.
It means "a sponsor is involved", which is a fact about the author that only
the GraphQL sponsorship query can establish, and `sponsor-label` applies it
from exactly that. Matching the *words* "funding" or "sponsor" (or a pull
request touching `.github/funding.yml`) labels the topic instead, so anyone
opening "Add a funding.yml" read as a sponsor. Precision-first means no rule
beats an ambiguous one when an authoritative source already exists.
```
"""

DEFAULT_FILE_RULES: dict[str, tuple[str, ...]] = {
    "🆙 changelog": (
        ".github/workflows/changelog.yaml",
        ".github/workflows/release.yaml",
        "changelog.md",
    ),
    "🤖 ci": (".github/**/*", ".gitignore", "pyproject.toml"),
    "🔗 dependencies": ("*.lock", "**/pyproject.toml"),
    "📚 documentation": (
        ".github/code-of-conduct.md",
        ".github/workflows/docs.yaml",
        ".mailmap",
        "docs/**/*",
        "license",
        "readme.md",
    ),
}
"""Default file rules: globs matched against the paths a pull request changes.

The dialect is `minimatch`'s (see {data}`GLOB_FLAGS`): `**` crosses
directories, `{a,b}` expands, a leading `!` subtracts from the label's other
globs, and a leading dot is matched like any other character. Keep the globs
precise: one broad enough to catch unrelated changes mislabels every pull
request touching them.
"""

CONTENT_PATTERN_RE = re.compile(r"^/(?P<body>.*)/(?P<flags>[a-z]*)$", re.DOTALL)
"""The `/body/flags` spelling a content pattern may take, mirroring JavaScript.

Matching this shape is what makes a pattern a regex: the body is passed to
`re` as written, and only the flags named between the slashes apply, so a
bare `/foo/` is case-sensitive. A pattern not in this shape is a literal
keyword instead, escaped and word-anchored and always matched
case-insensitively (see {func}`compile_content_pattern`). So the slashed form
is the one to reach for when a rule genuinely needs regex syntax, and the one
that has to spell `i` out to get back the case-insensitivity the bare form
gives for free.
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


def _resolve_rules(
    defaults: dict[str, tuple[str, ...]],
    overrides: object,
    kind: str,
) -> dict[str, tuple[str, ...]]:
    """Overlay a project's rule entries on the bundled defaults.

    An entry for a label the defaults also carry replaces the default entry
    wholesale, an empty list disables the label, and a bare string is accepted
    as a single-pattern list. Replacement is deliberate where an earlier
    revision merged: merging could only ever widen a rule, leaving no way to
    correct or silence a default, and restating a default's short pattern list
    is cheaper than a second override vocabulary.

    A non-mapping *overrides* value is the pre-`7.11.0` array-of-tables form,
    reported and then ignored so a stale project degrades to the defaults
    instead of crashing every command that loads its configuration.
    """
    if not isinstance(overrides, dict):
        if overrides:
            logging.warning(
                "Ignoring [tool.repomatic.labels] %s-rules: the array-of-tables"
                " form was replaced in 7.11.0 by a table mapping each label to"
                " its patterns. See the changelog for the migration.",
                kind,
            )
        overrides = {}
    resolved = dict(defaults)
    for raw_label, raw_patterns in overrides.items():
        label = str(raw_label).strip()
        if not label:
            logging.warning("Skipping %s rule with a blank label.", kind)
            continue
        if isinstance(raw_patterns, str):
            raw_patterns = [raw_patterns]
        if not isinstance(raw_patterns, list) or not all(
            isinstance(pattern, str) for pattern in raw_patterns
        ):
            logging.warning(
                "Skipping %s rule for label %r: expected a list of strings, got %r.",
                kind,
                label,
                raw_patterns,
            )
            continue
        if raw_patterns:
            resolved[label] = tuple(raw_patterns)
        else:
            # An explicit empty list is the disable switch for a default rule.
            logging.debug(f"Label {label!r} disabled by an empty {kind} rule.")
            resolved.pop(label, None)
    return resolved


def resolve_content_rules(config: Config | None = None) -> dict[str, tuple[str, ...]]:
    """The content rules in force: bundled defaults overlaid with the project's.

    :param config: The resolved `[tool.repomatic]` configuration, or `None`
        for the bundled defaults alone.
    :return: Patterns keyed by label.
    """
    overrides = config.labels.content_rules if config else {}
    return _resolve_rules(DEFAULT_CONTENT_RULES, overrides, "content")


def resolve_file_rules(config: Config | None = None) -> dict[str, tuple[str, ...]]:
    """The file rules in force: bundled defaults overlaid with the project's.

    :param config: The resolved `[tool.repomatic]` configuration, or `None`
        for the bundled defaults alone.
    :return: Glob patterns keyed by label.
    """
    overrides = config.labels.file_rules if config else {}
    return _resolve_rules(DEFAULT_FILE_RULES, overrides, "file")


@cache
def compile_content_pattern(pattern: str) -> re.Pattern[str] | None:
    r"""Compile one content pattern, as a keyword or a `/body/flags` regex.

    Memoized: the rule tables hand the same patterns to every matching call,
    so each spelling compiles once per process (and a malformed one is warned
    about once instead of on every thread it is matched against).

    A bare pattern is a literal keyword: escaped, matched case-insensitively,
    and word-anchored on each edge that is itself a word character, so `fix`
    does not fire inside `prefix` while `.lock` still matches the tail of
    `uv.lock` (a `\b` before the dot would demand a word character ahead of
    it). Case-insensitivity is the point of defaulting this way: users
    capitalize freely, and a convention every rule must remember to spell is a
    convention half of them forget.

    The `/body/flags` form passes the body through as a regex, mirroring
    JavaScript because that is what the retired `github/issue-labeler` action
    read and what existing rules are written in. No flags means
    case-sensitive.

    Returns `None` on a body the `re` module rejects, having logged it. A
    single malformed rule must not take the whole labelling run down with it:
    the job is a convenience that runs once per opened issue, and the other
    rules still have work to do.
    """
    if slashed := CONTENT_PATTERN_RE.match(pattern):
        body = slashed["body"]
        flags = 0
        for flag in slashed["flags"]:
            flags |= CONTENT_PATTERN_FLAGS.get(flag, 0)
    else:
        body = re.escape(pattern)
        if pattern and re.match(r"\w", pattern[0]):
            body = r"\b" + body
        if pattern and re.match(r"\w", pattern[-1]):
            body += r"\b"
        flags = re.IGNORECASE
    try:
        return re.compile(body, flags)
    except re.error as error:
        logging.warning("Skipping malformed content pattern %r: %s.", pattern, error)
        return None


def match_content_rules(rules: Mapping[str, Sequence[str]], text: str) -> set[str]:
    """Return every label with a pattern matching *text*.

    :param rules: Patterns keyed by label, from {func}`resolve_content_rules`.
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


def match_file_rules(
    rules: Mapping[str, Sequence[str]],
    files: Sequence[str],
) -> set[str]:
    """Return every label whose globs match a changed file.

    A label's globs are evaluated as one set, so a `!`-negated entry subtracts
    from its siblings (`["docs/**", "!docs/generated/**"]` reads the way a
    `.gitignore` would) rather than standing alone. A pull request that
    changes no files matches nothing.

    :param rules: Glob patterns keyed by label, from {func}`resolve_file_rules`.
    :param files: Paths the pull request changes, relative to the repo root.
    :return: The matching labels.
    """
    matched = set()
    for label, patterns in rules.items():
        pattern_set = list(patterns)
        if any(glob.globmatch(file, pattern_set, flags=GLOB_FLAGS) for file in files):
            logging.debug(f"File rule {patterns!r} matched {label!r}.")
            matched.add(label)
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

    The canonical token ({func}`~repomatic.github.gh.resolve_gh_token`) is
    injected as `GH_TOKEN`, which labelmaker reads, so an environment carrying
    only `REPOMATIC_PAT` still syncs authenticated — the same promotion every
    `gh` call gets.

    :param labelmaker_path: Path to the labelmaker binary.
    :param args: Arguments to pass to labelmaker.
    :raises RuntimeError: If labelmaker fails.
    """
    cmd = [str(labelmaker_path), *args]
    logging.info(f"Running: {' '.join(cmd)}")
    token = resolve_gh_token()
    env = {**os.environ, "GH_TOKEN": token} if token else None
    result = subprocess.run(
        cmd,
        capture_output=True,
        encoding="UTF-8",
        check=False,
        env=env,
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
