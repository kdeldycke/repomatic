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

"""Bundled Claude assets hold no drifting copy of a repository fact.

Skills and agents ship verbatim to every downstream repo through
`repomatic init`, and nothing else audits their bodies: `test_skills.py`
checks frontmatter against the Agent Skills spec, the release sweep reads
them by hand once a cycle. So a cooldown window, a config key or a module
path quoted in one of them is a second copy of something the package
already defines, free to drift the moment the original moves.

A second family of checks holds the same assets to the prose rule in
`claude.md` § Directives are written in Simplified Technical English: the
directive a rule opens with stays short enough to read once, while the
rationale following it is left alone.

Each drift check below pins one copy to its source, the way
`test_workflows.py::test_workflow_declares_cooldown_env` pins the
workflow-level cooldown block and `test_uv.py` pins `[tool.uv]
exclude-newer`. Matching is deliberately under-inclusive: every rule
keys off an unambiguous form (a quoted flag argument, a backtick span
opening on `[tool.repomatic`), so prose that merely mentions a key in
passing is skipped rather than guessed at. A missed reference costs
nothing; a false failure costs a maintainer the afternoon.

```{note}
The first run of {func}`test_config_keys_resolve` caught
`repomatic-audit` recommending `[tool.repomatic.workflow.ignore_paths]`
and `[tool.repomatic.workflow.extra_paths]`: the dataclass attributes are
snake_case, but the TOML keys are `workflow.ignore-paths` and
`workflow.extra-paths`, so a downstream repo following that advice got a
config that parsed, warned about unknown keys, and silently did nothing.
```
"""

from __future__ import annotations

import re

import pytest
import yaml
from click_extra import schema_field_infos

from repomatic.bundle import get_data_content
from repomatic.config import Config
from repomatic.frontmatter import split_frontmatter
from repomatic.prepare_release import SELF_PIN_COOLDOWN_EXEMPTION
from repomatic.registry import COMPONENTS_BY_NAME, SKILL_FILENAME

from .conftest import PROJECT_ROOT, WORKFLOWS_DIR

CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")
"""Inline code span, the only context these checks read.

Scoping to code spans is what keeps the rules from firing on prose. A
shell one-liner that greps for the literal string `[tool.repomatic]` sits
in a span of its own, and is skipped because the span does not *open* on
that table (see {data}`CONFIG_REF_RE`).
"""

CONFIG_REF_RE = re.compile(
    r"^\[tool\.repomatic(?P<table>(?:\.[a-z][a-z0-9_-]*)*)\]"
    r"(?:\s+(?P<key>[a-z][a-z0-9._-]*))?"
)
"""A `[tool.repomatic]` reference, in either form assets write it.

Both `[tool.repomatic] nuitka.enabled` and
`[tool.repomatic.workflow.paths]` name the same kind of thing, so the
table suffix and the trailing key are joined into one dotted path before
lookup. Anchored at the start of the span so it never matches mid-sentence.
"""

COOLDOWN_WINDOW_RE = re.compile(r"--exclude-newer[= ]'(?P<window>[^']+)'")
"""A `--exclude-newer` flag carrying a quoted duration.

The bare and double-quoted forms are absent from every bundled asset, so
this stays keyed on the one shape in use rather than accepting three.
"""

MODULE_PATH_RE = re.compile(r"^(repomatic/[a-z0-9_/]+\.py)$")
"""A package module quoted as a repository-relative path."""

WORKFLOW_FILE_RE = re.compile(r"^(_?[a-z][a-z0-9-]*\.yaml)$")
"""A workflow filename quoted on its own."""

WORKFLOW_JOB_RE = re.compile(
    r"`(?P<workflow>_?[a-z][a-z0-9-]*\.yaml)` workflow's `(?P<job>[a-z][a-z0-9-]*)` job"
)
"""An asset attributing a job to the workflow that declares it.

Matched across the prose rather than inside one code span, because the
claim spans two of them. The possessive phrasing is what makes it safe to
read as an assertion: a line merely listing several workflows and a job
name never takes this shape, so the rule stays as under-inclusive as its
siblings while still pinning the one form that states ownership.
"""


def bundled_assets() -> list[tuple[str, str]]:
    """Every bundled skill and agent, as `(asset id, body)` pairs.

    Skills are folders entered through `SKILL.md`; agents are single files.
    Both deploy verbatim, so both are held to the same rules.
    """
    assets = [
        (entry.file_id, get_data_content(f"{entry.source}/{SKILL_FILENAME}"))
        for entry in COMPONENTS_BY_NAME["skills"].files
    ]
    assets.extend(
        (entry.file_id, get_data_content(entry.source))
        for entry in COMPONENTS_BY_NAME["subagents"].files
    )
    return assets


bundled_asset = pytest.mark.parametrize(
    ("asset_id", "body"),
    bundled_assets(),
    ids=[asset_id for asset_id, _body in bundled_assets()],
)


@bundled_asset
def test_cooldown_window_matches_config(asset_id: str, body: str) -> None:
    """A quoted cooldown window equals `[tool.repomatic] minimum-release-age`.

    `claude.md` § Where the window comes from names two files allowed to
    carry the duration as a literal, both pinned by a test. Skills that
    hand a maintainer a `uvx` command are a third carrier, and the one
    nothing regenerates: raising the window in config would leave four
    skills quoting the old span at anyone who reads them.
    """
    for window in COOLDOWN_WINDOW_RE.findall(body):
        assert window == Config.minimum_release_age, (
            f"{asset_id} gates an install on a {window!r} cooldown, but "
            f"[tool.repomatic] minimum-release-age is "
            f"{Config.minimum_release_age!r}."
        )


@bundled_asset
def test_self_pin_exemption_matches_constant(asset_id: str, body: str) -> None:
    """The repomatic self-pin bypass is quoted exactly as the freeze emits it.

    A hand-typed variant (a different span, the package spelled with an
    underscore) reads as the documented exemption while gating nothing,
    and uv reports no error for an exemption naming a package it never
    resolves.
    """
    if "--exclude-newer-package" not in body:
        pytest.skip("asset declares no per-package cooldown exemption")
    assert SELF_PIN_COOLDOWN_EXEMPTION in body, (
        f"{asset_id} carries a per-package cooldown exemption that is not "
        f"{SELF_PIN_COOLDOWN_EXEMPTION!r} verbatim."
    )


@bundled_asset
def test_config_keys_resolve(asset_id: str, body: str) -> None:
    """Every `[tool.repomatic]` key an asset recommends exists in the schema.

    Downstream repos act on these verbatim, and an unknown key is close to
    silent: `load_repomatic_config` warns once, then the setting is simply
    absent, so the behaviour the asset promised never arrives.
    """
    declared = {info.key for info in schema_field_infos(Config)}
    for span in CODE_SPAN_RE.findall(body):
        match = CONFIG_REF_RE.match(span.strip())
        if not match:
            continue
        table = match.group("table").lstrip(".")
        dotted = ".".join(part for part in (table, match.group("key")) if part)
        if not dotted:
            continue
        assert dotted in declared, (
            f"{asset_id} recommends [tool.repomatic] {dotted}, which is not a "
            "declared config key. TOML keys are kebab-case, unlike the "
            "dataclass attributes they map to."
        )


@bundled_asset
def test_module_paths_exist(asset_id: str, body: str) -> None:
    """Every package module quoted as a path is still there under that name.

    Assets point at modules to say where a rule is implemented. A rename
    turns that into a dead end for whoever follows it, and the pointer is
    the whole value of the sentence carrying it.
    """
    for span in CODE_SPAN_RE.findall(body):
        match = MODULE_PATH_RE.match(span.strip())
        if not match:
            continue
        module = match.group(1)
        assert (PROJECT_ROOT / module).is_file(), (
            f"{asset_id} points at {module}, which no longer exists."
        )


@bundled_asset
def test_workflow_files_exist(asset_id: str, body: str) -> None:
    """Every workflow filename quoted in an asset is still a workflow.

    Assets name workflows constantly: which one to poll, which one runs a
    fix, which one to dispatch. A renamed or retired workflow leaves those
    sentences pointing at a file no `gh workflow run` will find.
    """
    for span in CODE_SPAN_RE.findall(body):
        match = WORKFLOW_FILE_RE.match(span.strip())
        if not match:
            continue
        workflow = match.group(1)
        assert (WORKFLOWS_DIR / workflow).is_file(), (
            f"{asset_id} names {workflow}, which is not a workflow file."
        )


@bundled_asset
def test_attributed_jobs_live_in_the_named_workflow(asset_id: str, body: str) -> None:
    """A job an asset attributes to a workflow is declared by that workflow.

    Both halves of the claim can outlive the claim itself: `update-docs`
    moved from `docs.yaml` to `autofix.yaml` in `5.7.1`, and the
    `sphinx-docs` agent kept crediting `docs.yaml` for eleven releases,
    because the workflow still existed and so did the job. Only the pairing
    was wrong, which is exactly what no existence check can see.
    """
    for match in WORKFLOW_JOB_RE.finditer(body):
        workflow, job = match.group("workflow"), match.group("job")
        path = WORKFLOWS_DIR / workflow
        assert path.is_file(), f"{asset_id} names {workflow}, which is not a workflow."
        jobs = yaml.safe_load(path.read_text(encoding="UTF-8")).get("jobs", {})
        assert job in jobs, (
            f"{asset_id} credits {workflow} with a {job!r} job, which it does not "
            f"declare. Its jobs are: {', '.join(sorted(jobs))}."
        )


DIRECTIVE_WORD_LIMIT = 25
"""Longest directive a bundled asset may carry, in words.

ASD-STE100 caps an instruction at 25 words and a description at 20. One
number covers both here, because a frontmatter `description` is an
instruction to the router deciding whether to load the asset at all.
"""

BOLD_LEAD_RE = re.compile(r"^\*\*(?P<lead>.+?)\*\*")
"""The bolded opening of a list item, which is where its directive sits.

The house style opens a rule bullet with the rule in bold, then follows it
with the rationale in plain prose. That split is what makes this checkable
at all: the bold span is held to the limit and the rationale after it is
not, so a rule can still explain itself at length.
"""

FENCE_RE = re.compile(r"^\s*```")
"""A fenced block delimiter. What sits between two of them is not prose."""

IMPERATIVE_VERBS = frozenset({
    "add",
    "always",
    "ask",
    "avoid",
    "call",
    "check",
    "commit",
    "confirm",
    "copy",
    "create",
    "delete",
    "do",
    "drop",
    "edit",
    "fix",
    "keep",
    "leave",
    "list",
    "make",
    "mark",
    "merge",
    "move",
    "name",
    "never",
    "open",
    "pass",
    "pick",
    "prefer",
    "push",
    "read",
    "record",
    "remove",
    "rename",
    "replace",
    "report",
    "rerun",
    "reset",
    "run",
    "set",
    "skip",
    "stop",
    "treat",
    "update",
    "use",
    "verify",
    "wait",
    "write",
})
"""Verbs that open a directive in a bullet carrying no bold lead.

Enumerated rather than inferred, which keeps the check under-inclusive by
construction: a bullet opening on a verb nobody listed is read as prose and
skipped. That is the same trade the drift checks above make, for the same
reason. A missed directive costs nothing; a false failure costs an
afternoon.
"""

LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+(?P<rest>.*)$")
"""A bullet or a numbered step, with whatever follows its marker."""

SENTENCE_END_RE = re.compile(r"^(?P<first>.+?[.:!?])(?:\s|$)")
"""The first sentence of a directive that carries no bold lead."""

URL_RE = re.compile(r"https?://\S+")
"""A bare URL, counted as one word rather than as its own punctuation."""


def word_count(text: str) -> int:
    """Words in a directive, counting a code span or a URL as one each.

    A directive quoting a long command is not a long sentence, and reads as
    one item to whoever follows it.
    """
    return len(URL_RE.sub("URL", CODE_SPAN_RE.sub("CODE", text)).split())


def directives(body: str) -> list[tuple[int, str]]:
    """Every directive in an asset body, as `(line number, text)` pairs.

    A directive is the bold lead of a list item, or the first sentence of a
    list item opening on an {data}`IMPERATIVE_VERBS` entry. Prose paragraphs
    and fenced code carry no directive and are skipped.
    """
    found = []
    in_fence = False
    for number, line in enumerate(body.splitlines(), 1):
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        item = LIST_ITEM_RE.match(line)
        if not item:
            continue
        rest = item.group("rest")
        bold = BOLD_LEAD_RE.match(rest)
        if bold:
            found.append((number, bold.group("lead")))
            continue
        first_word = re.match(r"[A-Za-z`][\w`'-]*", rest)
        if first_word and first_word.group().strip("`").lower() in IMPERATIVE_VERBS:
            sentence = SENTENCE_END_RE.match(rest)
            found.append((number, sentence.group("first") if sentence else rest))
    return found


@bundled_asset
def test_description_is_short_sentences(asset_id: str, body: str) -> None:
    """Every sentence of a frontmatter `description` clears the word limit.

    The description is the only part of an asset read on every turn, since
    it is what the router matches against to decide whether to load the rest.
    A 56-word run-on there costs matching accuracy on every session, which is
    why this is the one place the limit binds unconditionally.
    """
    meta, _body = split_frontmatter(body)
    description = meta.get("description", "")
    for sentence in re.split(r"(?<=[.;])\s+", description):
        if not sentence.strip():
            continue
        assert word_count(sentence) <= DIRECTIVE_WORD_LIMIT, (
            f"{asset_id} opens with a {word_count(sentence)}-word sentence in "
            f"its description, over the {DIRECTIVE_WORD_LIMIT}-word limit. "
            f"Split it: {sentence!r}"
        )


@bundled_asset
def test_directives_are_short(asset_id: str, body: str) -> None:
    """Every directive in an asset body clears the word limit.

    Only the directive is measured, never the rationale that follows it, so
    a rule keeps as much room to explain itself as it needs. A directive
    over the limit is nearly always two rules sharing a bullet, or a rule
    with its exception folded in: both read better split.
    """
    _meta, content = split_frontmatter(body)
    for number, directive in directives(content):
        assert word_count(directive) <= DIRECTIVE_WORD_LIMIT, (
            f"{asset_id}:{number} carries a {word_count(directive)}-word "
            f"directive, over the {DIRECTIVE_WORD_LIMIT}-word limit. Split "
            f"the rule from its exception, or move the detail into the "
            f"rationale after it: {directive!r}"
        )
