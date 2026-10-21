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

"""Tests for the spellings of the upstream toolkit's pin on itself."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from repomatic.config import Config
from repomatic.deps.sync import ResolveContext, _resolve_workflow_pins
from repomatic.lint_repo.context import LintContext
from repomatic.lint_repo.pins import (
    check_inline_pins_match_upstream,
    check_self_pin_cooldown_exemption,
)
from repomatic.release.self_pin import (
    add_cooldown_exemption,
    align_inline_pins,
    cooldown_exemption,
    cooldown_exemptions,
    find_inline_pins,
    frozen_cli_invocation,
    frozen_cli_re,
    local_cli_invocation,
)
from repomatic.upstream import DEFAULT_REPO, UPSTREAM_PACKAGE

UPSTREAM_REF = (
    "jobs:\n"
    "  lint:\n"
    f"    uses: {DEFAULT_REPO}/.github/workflows/lint.yaml"
    "@1234567890abcdef1234567890abcdef12345678 # v6.29.0\n"
)
"""A SHA-pinned thin-caller ref to the upstream toolkit, declaring `v6.29.0`."""

PIN_SHAPES = (
    pytest.param("uvx --no-progress 'repomatic=={}' show-metadata", id="quoted-uvx"),
    pytest.param("uvx repomatic=={} show-metadata", id="bare-uvx"),
    pytest.param("uv tool install 'repomatic=={}'", id="tool-install"),
    pytest.param("uvx --with 'repomatic[cli]=={}' papaya", id="with-extras"),
    pytest.param("pip install repomatic=={}", id="pip-install"),
)
"""Every shape a workflow writes the self-pin in, with a `{}` for the version."""


def _lint_context(text: str) -> LintContext:
    """A lint context holding one workflow file, *text*, and none on disk."""
    ctx = LintContext()
    ctx.__dict__["workflow_texts"] = {Path(".github/workflows/ci.yaml"): text}
    return ctx


@pytest.mark.parametrize("shape", PIN_SHAPES)
def test_sync_fixes_every_pin_the_lint_flags(shape: str) -> None:
    """`sync-workflow-pins` rewrites each pin shape the lockstep check reads.

    A shape the check reads and the rewrite skips leaves `lint-repo` red until a
    human edits the workflow, with no pull request to say why.
    """
    text = UPSTREAM_REF + f"      - run: {shape.format('6.28.1')}\n"
    assert check_inline_pins_match_upstream(_lint_context(text)).passed is False

    rc = ResolveContext(config=Config(), today=date(2026, 10, 20))
    rc.__dict__["pinnable_files"] = {Path(".github/workflows/ci.yaml"): text}
    plan = _resolve_workflow_pins(rc)

    assert ("repomatic", "6.28.1", "6.29.0") in plan.changes
    (written,) = plan.file_writes.values()
    assert check_inline_pins_match_upstream(_lint_context(written)).passed is True


@pytest.mark.parametrize("shape", PIN_SHAPES)
def test_inline_pin_shapes_are_read(shape: str) -> None:
    """Every shape a workflow writes the pin in is one the matcher reads."""
    assert find_inline_pins(shape.format("1.2.3"), "repomatic") == ["1.2.3"]


@pytest.mark.parametrize(
    "text",
    (
        pytest.param("uvx 'papaya-repomatic==1.2.3'", id="longer-name"),
        pytest.param("--exclude-newer-package repomatic=P0D", id="exemption"),
        pytest.param("uvx 'repomatic>=1.2.3'", id="floor"),
    ),
)
def test_inline_pin_matcher_ignores_other_text(text: str) -> None:
    """A name ending in the package, the exemption, and a floor pin nothing."""
    assert find_inline_pins(text, "repomatic") == []


@pytest.mark.parametrize(
    ("content", "expected", "changes"),
    (
        pytest.param(
            "uvx 'repomatic==1.0.0' and pip install repomatic[cli]==1.1.0",
            "uvx 'repomatic==2.0.0' and pip install repomatic[cli]==2.0.0",
            [("repomatic", "1.0.0", "2.0.0"), ("repomatic", "1.1.0", "2.0.0")],
            id="behind",
        ),
        pytest.param(
            "uvx 'repomatic==3.0.0'",
            "uvx 'repomatic==2.0.0'",
            [("repomatic", "3.0.0", "2.0.0")],
            id="ahead",
        ),
        pytest.param(
            "uvx 'repomatic==2.0.0'", "uvx 'repomatic==2.0.0'", [], id="equal"
        ),
    ),
)
def test_align_inline_pins(
    content: str, expected: str, changes: list[tuple[str, str, str]]
) -> None:
    """Every pin moves onto the version, extras kept, and each move is reported."""
    assert align_inline_pins(content, "repomatic", "2.0.0") == (expected, changes)


def test_cooldown_exemption_names_its_package() -> None:
    """A fork under another name gets an exemption for its own package."""
    assert cooldown_exemption("fig") == "--exclude-newer-package fig=P0D"


def test_frozen_invocation_is_what_the_unfreeze_matches() -> None:
    """The freeze writes one spelling, and the unfreeze recognizes it."""
    frozen = frozen_cli_invocation("fig", "1.2.3")
    assert frozen_cli_re("fig").fullmatch(frozen)
    assert frozen_cli_re("fig").sub(local_cli_invocation("fig"), frozen) == (
        "uv --no-progress run --frozen -- fig"
    )


def test_spliced_exemption_converges_on_the_frozen_spelling() -> None:
    """A bare `uvx` pin gains the exemption the freeze writes, and only once."""
    bare = "uvx --no-progress 'fig==1.2.3' show-metadata"
    spliced = add_cooldown_exemption(bare, "fig")
    assert spliced == f"{frozen_cli_invocation('fig', '1.2.3')} show-metadata"
    assert add_cooldown_exemption(spliced, "fig") == spliced
    assert cooldown_exemptions(bare, "fig") == [False]
    assert cooldown_exemptions(spliced, "fig") == [True]


@pytest.mark.parametrize("exempt", (False, True))
def test_lint_reads_the_exemption_the_splice_writes(exempt: bool) -> None:
    """The exemption check and the splice agree on what an exempt command is."""
    command = "uvx --no-progress 'repomatic==6.29.0' show-metadata"
    if exempt:
        command = add_cooldown_exemption(command, UPSTREAM_PACKAGE)
    ctx = _lint_context(f'env:\n  UV_EXCLUDE_NEWER: "1 week"\n      - run: {command}\n')
    assert check_self_pin_cooldown_exemption(ctx).passed is exempt
