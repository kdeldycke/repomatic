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
The schema says "label X when any changed file matches any of these globs" or
"when any of these words appears", and nothing more. Per-matcher quantifiers,
branch regexes and `any`/`all` group nesting are left out on purpose: no rule
of a repository this toolkit manages needs them.
```
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import tempfile
from dataclasses import dataclass
from functools import cache
from pathlib import Path, PurePosixPath

import tomlrt
import yaml
from wcmatch import glob

from ..bundle import get_data_content
from ..http import FetchError, get_bytes
from ..tooling.runner import ensure_binary
from .event import get_event_subject
from .gh import gh_env, gh_json
from .pr import list_changed_files
from .threads import ThreadKind, repo_args

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence
    from typing import Any

    from ..config import Config

DEFAULT_CONTENT_RULES: dict[str, tuple[str, ...]] = {
    "🐛 bug": ("bug", "error", "exception", "fix", "traceback"),
    "🆙 changelog": ("change-log", "changelog"),
    "🤖 ci": (
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
`🤖 ci` deliberately does not match `.github`, which is the case the rule
above names. Every issue form {mod}`repomatic` bundles for an awesome list
links its own `.github/code-of-conduct.md` and `.github/contributing.md` in
the self-check boxes, and GitHub renders those option labels into the filed
body verbatim, so the keyword pre-labelled every submission filed through one.
A pull request that actually touches those paths is already covered by the
`.github/**/*` glob in {data}`DEFAULT_FILE_RULES`, and prose about CI still
matches `workflow`, `actions` or `ci-cd`.
```

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
migration, not a deletion" in the `github-housekeeping` skill."""


def _resolve_rules(
    defaults: dict[str, tuple[str, ...]],
    overrides: object,
    kind: str,
) -> dict[str, tuple[str, ...]]:
    """Overlay a project's rule entries on the bundled defaults.

    An entry for a label the defaults also carry replaces the default entry
    wholesale, an empty list disables the label, and a bare string is accepted
    as a single-pattern list. Replacement, not merging: a merge could only ever
    widen a rule, leaving no way to correct or silence a default, and restating
    a default's short pattern list is cheaper than a second override
    vocabulary.

    A *overrides* value that is not a table is reported and then ignored, so a
    malformed project degrades to the defaults instead of crashing every
    command that loads its configuration.
    """
    if not isinstance(overrides, dict):
        if overrides:
            logging.warning(
                f"Ignoring [tool.repomatic.labels] {kind}-rules: expected a table "
                "mapping each label to its patterns."
            )
        overrides = {}
    resolved = dict(defaults)
    for raw_label, raw_patterns in overrides.items():
        label = str(raw_label).strip()
        if not label:
            logging.warning(f"Skipping {kind} rule with a blank label.")
            continue
        if isinstance(raw_patterns, str):
            raw_patterns = [raw_patterns]
        if not isinstance(raw_patterns, list) or not all(
            isinstance(pattern, str) for pattern in raw_patterns
        ):
            logging.warning(
                f"Skipping {kind} rule for label {label!r}: expected a list of strings, "
                f"got {raw_patterns!r}."
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

    The `/body/flags` form passes the body through as a regex, in the
    JavaScript spelling existing rules are written in. No flags means
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
        logging.warning(f"Skipping malformed content pattern {pattern!r}: {error}.")
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


def labels_for_thread(
    config: Config,
    number: int,
    *,
    kind: ThreadKind,
    repository: str | None = None,
) -> list[str]:
    """The labels the project's rules give one issue or pull request.

    In the labeller job the event payload carries the thread. A thread the
    payload does not carry has its text read from the API instead, so the
    content rules see the real title and body rather than an empty string.
    File rules need a diff, which only a pull request has.

    :param config: The resolved `[tool.repomatic]` configuration.
    :param number: The issue or pull request number.
    :param kind: Whether *number* names an issue or a pull request.
    :param repository: Repository in `owner/name` form, or `None` to let `gh`
        resolve it.
    :return: The matched labels, sorted.
    """
    subject = get_event_subject()
    if subject.get("number") != number:
        args = [
            kind,
            "view",
            str(number),
            *repo_args(repository),
            "--json",
            "title,body",
        ]
        subject = gh_json(args, soft=True) or {}
    text = f"{subject.get('title') or ''}\n{subject.get('body') or ''}"
    matched = match_content_rules(resolve_content_rules(config), text)
    if kind is ThreadKind.PR:
        matched |= match_file_rules(
            resolve_file_rules(config), list_changed_files(number, repository or "")
        )
    return sorted(matched)


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
            logging.warning(f"Skipping inline label without a `name`: {entry!r}.")
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
                f"Ignoring unknown fields {', '.join(map(repr, unknown))} on inline "
                f"label {name!r}."
            )
        labels.append(label)

    if not labels:
        return ""

    doc = tomlrt.Document({"profiles": {"default": {"labels": labels}}})
    return tomlrt.dumps(doc)


EXTRA_LABELS_DIR = "extra-labels"
"""Directory of a repository holding its hand-written label definitions."""


def extra_label_filename(url: str) -> str:
    """The filename a `labels.extra-files` download is saved under.

    Shared by the download of `repomatic init labels` and by
    {func}`label_sources`, which keys a fetched definition the same way, so it
    shadows a committed `extra-labels/` file of that name.

    :param url: One `labels.extra-files` entry.
    :return: The last segment of the URL path.
    """
    return PurePosixPath(url.strip()).name


@dataclass(frozen=True)
class LabelSource:
    """One labelmaker definition file, as `sync-labels` applies it."""

    path: str
    """Where the file is staged for labelmaker, relative to the staging root.

    Its extension tells labelmaker the format.
    """

    text: str
    """The definitions."""

    profiles: tuple[str, ...] | None = None
    """The profiles applied, one `--profile` each. `None` applies the file
    without one, and reads every profile it declares."""


def label_sources(
    config: Config,
    *,
    is_awesome: bool,
    root: Path | None = None,
) -> Iterator[LabelSource]:
    """Every label definition the configuration applies to a repository.

    In apply order: the bundled `labels.toml` under the `default` profile (and
    the `awesome` one for an `awesome-*` repository), the hand-written files of
    `extra-labels/` and the `labels.extra-files` downloads, then the inline
    `[tool.repomatic.labels.extra]` definitions. Both {func}`apply_labels` and
    {func}`declared_label_names` read this one list, so a label the sync
    creates is never reported as undeclared.

    :param config: The resolved `[tool.repomatic]` configuration.
    :param is_awesome: Whether the repository is an `awesome-*` list.
    :param root: Repository root holding `extra-labels/`, the current
        directory when `None`.
    :yields: Each definition file.
    :raises repomatic.http.FetchError: When a `labels.extra-files` URL cannot
        be fetched. The list would otherwise come back short, and every label
        only that file declares would be lost.
    """
    yield LabelSource(
        "labels.toml",
        get_data_content("labels.toml"),
        ("default", "awesome") if is_awesome else ("default",),
    )

    extra_dir = (Path() if root is None else root) / EXTRA_LABELS_DIR
    texts: dict[str, str] = {}
    if extra_dir.is_dir():
        for label_file in sorted(extra_dir.iterdir()):
            if label_file.is_file():
                texts[label_file.name] = label_file.read_text(encoding="UTF-8")
    # Keyed by filename, so a download shadows a committed file of that name.
    for entry in config.labels.extra_files:
        url = entry.strip()
        if not url:
            continue
        try:
            body = get_bytes(url)
        except FetchError as exc:
            # The message gains the URL: the bare failure names no file.
            msg = f"{url}: {exc}"
            raise FetchError(msg, status=exc.status, body=exc.body) from exc
        texts[extra_label_filename(url)] = body.decode("UTF-8", errors="replace")
    for filename, text in sorted(texts.items()):
        yield LabelSource(f"{EXTRA_LABELS_DIR}/{filename}", text)

    inline = serialize_inline_labels(config.labels.extra)
    if inline:
        yield LabelSource("inline.toml", inline)


class LabelConfigError(ValueError):
    """A label definition file that {func}`declared_label_names` cannot read."""


def _load_labelmaker_config(text: str, filename: str) -> dict[str, Any]:
    """Parse a labelmaker config in the format its file extension names.

    `labelmaker` picks the format from the extension, case-insensitively:
    `.json`, `.json5`, `.toml`, `.yaml` or `.yml`, and it refuses any other.
    No JSON5 parser ships with this package, so a `.json5` file is read as
    JSON: plain JSON is valid JSON5, and anything beyond that subset is
    reported as unreadable rather than guessed at.

    :param text: Contents of the config file.
    :param filename: Name of the file, whose extension selects the format.
    :return: The top-level mapping of the config.
    :raises LabelConfigError: When the extension names no format `labelmaker`
        reads, or the text does not parse to a mapping in that format.
    """
    suffix = PurePosixPath(filename).suffix.lower()
    if suffix not in {".json", ".json5", ".toml", ".yaml", ".yml"}:
        msg = f"{filename}: labelmaker reads no {suffix or 'extensionless'} file."
        raise LabelConfigError(msg)
    # YAML and JSON can hold any top-level value, checked below.
    data: Any
    try:
        if suffix == ".toml":
            data = tomlrt.loads(text).to_dict()
        elif suffix in {".yaml", ".yml"}:
            data = yaml.safe_load(text)
        else:
            data = json.loads(text)
    except (ValueError, yaml.YAMLError, tomlrt.TOMLParseError) as exc:
        msg = f"{filename}: {exc}"
        raise LabelConfigError(msg) from exc
    if not isinstance(data, dict):
        msg = f"{filename}: the top level is not a mapping."
        raise LabelConfigError(msg)
    return data


def _names_in_labelmaker_config(
    text: str, profiles: Sequence[str] | None = None, *, filename: str = "labels.toml"
) -> set[str]:
    """Every label name a labelmaker config declares, across its profiles.

    Walks `profiles.*.labels[].name`, so the same reader serves the bundled
    `labels.toml` (which carries `default` and `awesome`), an `extra-labels/`
    file in any format {func}`_load_labelmaker_config` reads, and the inline
    block {func}`serialize_inline_labels` emits.

    A `rename-from` entry is deliberately **not** a declaration: it names a
    label expected to stop existing, so counting it would hide the very
    orphan {func}`~repomatic.lint_repo.check_undeclared_labels` looks for.

    :param text: Contents of a labelmaker config.
    :param profiles: IDs of the profiles to read, like the `--profile` values
        {func}`apply_labels` passes. `None` reads every profile.
    :param filename: Name of the file the text came from. Its extension
        selects the format.
    :return: The declared label names.
    :raises LabelConfigError: When the text cannot be read, per
        {func}`_load_labelmaker_config`.
    """
    data = _load_labelmaker_config(text, filename)
    names: set[str] = set()
    for profile_id, profile in (data.get("profiles") or {}).items():
        if profiles is not None and profile_id not in profiles:
            continue
        if not isinstance(profile, dict):
            continue
        for label in profile.get("labels") or ():
            if not isinstance(label, dict):
                continue
            name = str(label.get("name", "")).strip()
            if name:
                names.add(name)
    return names


def declared_label_names(
    config: Config,
    *,
    is_awesome: bool,
    labels_dir: Path | None = None,
) -> set[str]:
    """Every label name the configured sources declare for a repository.

    Reads the {func}`label_sources` that {func}`apply_labels` applies, which is
    what lets {func}`~repomatic.lint_repo.check_undeclared_labels` call
    anything outside this set an orphan.

    :param config: The resolved `[tool.repomatic]` configuration.
    :param is_awesome: Whether the repository is an `awesome-*` list, which
        adds the `awesome` profile on top of `default`.
    :param labels_dir: Repository root holding `extra-labels/`, the current
        directory when `None`.
    :return: The declared label names.
    :raises repomatic.http.FetchError: When a `labels.extra-files` URL cannot
        be fetched.
    :raises LabelConfigError: When an `extra-labels/` or `labels.extra-files`
        definition cannot be read. The set would otherwise come back short.
    """
    names: set[str] = set()
    for source in label_sources(config, is_awesome=is_awesome, root=labels_dir):
        names |= _names_in_labelmaker_config(
            source.text, source.profiles, filename=PurePosixPath(source.path).name
        )
    return names


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
    env = gh_env()
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
    root: Path | None = None,
) -> None:
    """Apply every configured label source to *repository* via `labelmaker`.

    Stages each of the {func}`label_sources` in a scratch directory, so a sync
    leaves the working tree untouched, then applies them in order.

    :param config: The resolved `[tool.repomatic]` configuration.
    :param repository: GitHub repository in `owner/name` form.
    :param is_awesome: Whether the repository is an `awesome-*` list.
    :param root: Repository root holding `extra-labels/`, the current directory
        when `None`.
    :raises RuntimeError: When a labelmaker invocation fails, or when a
        `labels.extra-files` URL cannot be fetched.
    """
    sources = list(label_sources(config, is_awesome=is_awesome, root=root))
    lm = ensure_binary("labelmaker")
    with tempfile.TemporaryDirectory(prefix="repomatic-labels-") as tmpdir:
        for source in sources:
            staged = Path(tmpdir) / source.path
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_text(source.text, encoding="UTF-8")
            if source.profiles is None:
                _run_labelmaker(lm, "apply", str(staged), repository)
                continue
            for profile in source.profiles:
                _run_labelmaker(
                    lm, "apply", str(staged), "--profile", profile, repository
                )
