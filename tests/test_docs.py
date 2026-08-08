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

"""Tests for the `update-docs` orchestration."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from click_extra import ClickException

from repomatic.config import Config, DocsConfig
from repomatic.docs import _run_docs_tool, update_docs, validate_docs_script_path


def _docs_config(update_script: str = "") -> Config:
    """Build a Config carrying just the `docs` fields under test.

    `update_script` defaults to empty here (not the shipped
    `./docs/docs_update.py`) so orchestration tests skip the script phase
    unless they opt in.
    """
    return Config(docs=DocsConfig(update_script=update_script))


def test_validate_docs_script_path_empty_returns_none(tmp_path):
    """An empty configured path disables the script phase."""
    assert validate_docs_script_path("", tmp_path) is None


def test_validate_docs_script_path_valid(tmp_path):
    """A `.py` file under `docs/` resolves to its absolute path."""
    resolved = validate_docs_script_path("docs/docs_update.py", tmp_path)
    assert resolved == (tmp_path / "docs" / "docs_update.py").resolve()


@pytest.mark.parametrize(
    ("script", "match"),
    (
        pytest.param("../outside.py", "escapes repository root", id="escapes-root"),
        pytest.param("scripts/build.py", "must be under docs/", id="outside-docs"),
        pytest.param("docs/notes.txt", "must be a .py file", id="not-python"),
    ),
)
def test_validate_docs_script_path_rejects(tmp_path, script, match):
    """Paths outside `docs/`, outside the repo, or non-`.py` are rejected."""
    with pytest.raises(ClickException, match=match):
        validate_docs_script_path(script, tmp_path)


def test_run_docs_tool_builds_frozen_docs_group_command():
    """The command runs the tool through a frozen uv with the `docs` group."""
    with patch(
        "repomatic.docs.subprocess.run",
        return_value=SimpleNamespace(returncode=0),
    ) as mock_run:
        _run_docs_tool("sphinx-apidoc", "sphinx-apidoc", "--no-toc")

    cmd = mock_run.call_args.args[0]
    assert cmd == [
        "uv",
        "--no-progress",
        "run",
        "--frozen",
        "--group",
        "docs",
        "--",
        "sphinx-apidoc",
        "--no-toc",
    ]


def test_run_docs_tool_raises_on_failure():
    """A non-zero exit code raises a `ClickException` naming the phase."""
    with (
        patch(
            "repomatic.docs.subprocess.run",
            return_value=SimpleNamespace(returncode=2),
        ),
        pytest.raises(ClickException, match="sphinx-apidoc failed with exit code 2"),
    ):
        _run_docs_tool("sphinx-apidoc", "sphinx-apidoc")


def test_update_docs_noop_without_sphinx(tmp_path, monkeypatch):
    """A project with no Sphinx configuration runs no docs tooling."""
    monkeypatch.chdir(tmp_path)
    meta = SimpleNamespace(is_sphinx=False)
    with (
        patch("repomatic.docs.Metadata", return_value=meta),
        patch("repomatic.docs._run_docs_tool") as mock_tool,
    ):
        update_docs(_docs_config())

    mock_tool.assert_not_called()


def test_update_docs_runs_apidoc_for_autodoc_project(tmp_path, monkeypatch):
    """An autodoc-enabled Sphinx project regenerates its apidoc stubs."""
    (tmp_path / "docs").mkdir()
    monkeypatch.chdir(tmp_path)
    meta = SimpleNamespace(is_sphinx=True, active_autodoc=True, uses_myst=True)
    with (
        patch("repomatic.docs.Metadata", return_value=meta),
        patch("repomatic.docs.convert_rst_files_in_directory", return_value=[]),
        patch("repomatic.docs._run_docs_tool") as mock_tool,
    ):
        update_docs(_docs_config())

    # Only the sphinx-apidoc phase runs: no update script, no directive blocks.
    assert mock_tool.call_count == 1
    assert mock_tool.call_args.args[0] == "sphinx-apidoc"


def test_update_docs_skips_apidoc_without_active_autodoc(tmp_path, monkeypatch):
    """Without an active autodoc extension, no docs tooling runs."""
    (tmp_path / "docs").mkdir()
    monkeypatch.chdir(tmp_path)
    meta = SimpleNamespace(is_sphinx=True, active_autodoc=False, uses_myst=False)
    with (
        patch("repomatic.docs.Metadata", return_value=meta),
        patch("repomatic.docs._run_docs_tool") as mock_tool,
    ):
        update_docs(_docs_config())

    mock_tool.assert_not_called()


def test_update_docs_check_skips_writes_and_propagates(tmp_path, monkeypatch):
    """`check=True` skips the write phases and runs the rest in check mode."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "docs_update.py").write_text("", encoding="UTF-8")
    (docs / "page.md").write_text(
        "<!-- mirror -->\n\n<!-- mirror-end -->\n", encoding="UTF-8"
    )
    monkeypatch.chdir(tmp_path)
    meta = SimpleNamespace(is_sphinx=True, active_autodoc=True, uses_myst=True)
    with (
        patch("repomatic.docs.Metadata", return_value=meta),
        patch(
            "repomatic.docs.convert_rst_files_in_directory", return_value=[]
        ) as mock_convert,
        patch("repomatic.docs._run_docs_tool", return_value=0) as mock_tool,
    ):
        update_docs(_docs_config("docs/docs_update.py"), check=True)

    # Phases 1-2 (which write) are skipped: no RST conversion, no apidoc.
    mock_convert.assert_not_called()
    labels = [call.args[0] for call in mock_tool.call_args_list]
    assert "sphinx-apidoc" not in labels
    # Phases 3-4 run in check mode, each forwarding the --check flag.
    assert mock_tool.call_count == 2
    for call in mock_tool.call_args_list:
        assert call.kwargs.get("check") is True
        assert "--check" in call.args


def test_update_docs_check_raises_on_drift(tmp_path, monkeypatch):
    """A non-zero exit from a check phase raises with an "out of date" message."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "docs_update.py").write_text("", encoding="UTF-8")
    monkeypatch.chdir(tmp_path)
    meta = SimpleNamespace(is_sphinx=True, active_autodoc=False, uses_myst=False)
    with (
        patch("repomatic.docs.Metadata", return_value=meta),
        patch("repomatic.docs._run_docs_tool", return_value=1),
        pytest.raises(ClickException, match="out of date"),
    ):
        update_docs(_docs_config("docs/docs_update.py"), check=True)
