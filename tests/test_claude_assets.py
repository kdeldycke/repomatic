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

Each check below pins one such copy to its source, the way
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
from click_extra import schema_field_infos

from repomatic.bundle import get_data_content
from repomatic.config import Config
from repomatic.prepare_release import SELF_PIN_COOLDOWN_EXEMPTION
from repomatic.registry import COMPONENTS_BY_NAME, SKILL_FILENAME

from .conftest import PROJECT_ROOT

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
        for entry in COMPONENTS_BY_NAME["agents"].files
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
