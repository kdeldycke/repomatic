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

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import tomlrt

from repomatic import labels as labels_module
from repomatic.config import Config
from repomatic.labels import apply_labels, serialize_inline_labels


def test_serialize_inline_labels_empty():
    """An empty input yields an empty string so the caller can skip labelmaker."""
    assert serialize_inline_labels([]) == ""


def test_serialize_inline_labels_all_invalid_returns_empty(caplog):
    """If every entry is nameless, the result is empty and warnings are logged."""
    caplog.set_level(logging.WARNING)
    entries = [{"color": "bfdadc"}, {"description": "no name here"}]
    assert serialize_inline_labels(entries) == ""
    assert len(caplog.records) == 2
    for record in caplog.records:
        assert "Skipping inline label without a `name`" in record.message


def test_serialize_inline_labels_round_trip():
    """Output parses back to the expected labelmaker structure."""
    entries = [
        {"name": "bug", "color": "d73a4a", "description": "Something isn't working"},
        {"name": "📦 manager: apk", "color": "bfdadc", "description": "apk"},
    ]
    output = serialize_inline_labels(entries)
    parsed = tomlrt.loads(output)
    labels = parsed["profiles"]["default"]["labels"]
    assert len(labels) == 2
    assert labels[0]["name"] == "bug"
    assert labels[0]["color"] == "d73a4a"
    assert labels[0]["description"] == "Something isn't working"
    assert labels[1]["name"] == "📦 manager: apk"


def test_serialize_inline_labels_strips_leading_hash_on_color():
    """Hex colors with a leading `#` are normalized to bare hex."""
    output = serialize_inline_labels([
        {"name": "bug", "color": "#d73a4a", "description": "x"},
    ])
    parsed = tomlrt.loads(output)
    assert parsed["profiles"]["default"]["labels"][0]["color"] == "d73a4a"


def test_serialize_inline_labels_omits_missing_optional_fields():
    """Missing `color` and `description` are omitted, not emitted as empty strings."""
    output = serialize_inline_labels([{"name": "bug"}])
    parsed = tomlrt.loads(output)
    label = parsed["profiles"]["default"]["labels"][0]
    assert label["name"] == "bug"
    assert "color" not in label
    assert "description" not in label


def test_serialize_inline_labels_passes_through_labelmaker_fields():
    """Every per-label labelmaker field rides through, booleans included."""
    output = serialize_inline_labels([
        {
            "name": "🔌 plugin",
            "color": "fef2c0",
            "description": "Plugin code",
            "create": False,
            "update": True,
            "enforce-case": False,
            "rename-from": ["🔌 bar-plugin", "plugin"],
            "on-rename-clash": "error",
        },
    ])
    parsed = tomlrt.loads(output)
    label = parsed["profiles"]["default"]["labels"][0]
    assert label["create"] is False
    assert label["update"] is True
    assert label["enforce-case"] is False
    assert label["rename-from"] == ["🔌 bar-plugin", "plugin"]
    assert label["on-rename-clash"] == "error"


def test_serialize_inline_labels_strips_hash_on_multi_color():
    """Multi-color lists are normalized to bare hex like single colors."""
    output = serialize_inline_labels([
        {"name": "bug", "color": ["#d73a4a", "bfdadc"]},
    ])
    parsed = tomlrt.loads(output)
    assert parsed["profiles"]["default"]["labels"][0]["color"] == [
        "d73a4a",
        "bfdadc",
    ]


def test_serialize_inline_labels_omits_empty_values():
    """Empty strings and empty lists are omitted, not emitted as blanks."""
    output = serialize_inline_labels([
        {"name": "bug", "description": "", "rename-from": []},
    ])
    parsed = tomlrt.loads(output)
    label = parsed["profiles"]["default"]["labels"][0]
    assert "description" not in label
    assert "rename-from" not in label


def test_serialize_inline_labels_warns_on_unknown_fields(caplog):
    """Unknown fields are dropped with a warning instead of aborting the sync."""
    caplog.set_level(logging.WARNING)
    output = serialize_inline_labels([
        {"name": "bug", "color": "d73a4a", "renme-from": ["typo"], "bogus": 1},
    ])
    parsed = tomlrt.loads(output)
    label = parsed["profiles"]["default"]["labels"][0]
    assert label["name"] == "bug"
    assert "renme-from" not in label
    assert "bogus" not in label
    assert any(
        "Ignoring unknown fields 'bogus', 'renme-from' on inline label 'bug'"
        in r.message
        for r in caplog.records
    )


def test_serialize_inline_labels_skips_blank_name(caplog):
    """A whitespace-only name is treated as missing."""
    caplog.set_level(logging.WARNING)
    output = serialize_inline_labels([
        {"name": "   ", "color": "bfdadc"},
        {"name": "real", "color": "bfdadc"},
    ])
    parsed = tomlrt.loads(output)
    labels = parsed["profiles"]["default"]["labels"]
    assert len(labels) == 1
    assert labels[0]["name"] == "real"
    assert any(
        "Skipping inline label without a `name`" in r.message for r in caplog.records
    )


@pytest.mark.parametrize(
    ("name", "description"),
    [
        ('quoted "label"', "has \"quotes\" and 'apostrophes'"),
        ("path\\separator", "back\\slash"),
        ("newline\ncontent", "tab\there"),
    ],
)
def test_serialize_inline_labels_escapes_special_characters(name, description):
    """Special characters survive serialization and re-parsing."""
    output = serialize_inline_labels([
        {"name": name, "color": "bfdadc", "description": description},
    ])
    parsed = tomlrt.loads(output)
    label = parsed["profiles"]["default"]["labels"][0]
    assert label["name"] == name
    assert label["description"] == description


# --- apply_labels path handling ---


@pytest.fixture
def captured_labelmaker(monkeypatch):
    """Record every labelmaker invocation instead of running the binary."""
    calls: list[tuple[str, ...]] = []

    monkeypatch.setattr(
        labels_module,
        "ensure_binary",
        lambda _name: Path("/fake/labelmaker"),
    )
    monkeypatch.setattr(
        labels_module,
        "_run_labelmaker",
        lambda _lm, *args: calls.append(args),
    )
    return calls


def test_apply_labels_reads_labels_toml_from_labels_dir(
    tmp_path, monkeypatch, captured_labelmaker
):
    """`labels_dir` decides where `labels.toml` is read from, not the CWD."""
    monkeypatch.chdir(tmp_path)
    export = tmp_path / "export"
    export.mkdir()

    apply_labels(Config(), "owner/repo", is_awesome=False, labels_dir=export)

    assert captured_labelmaker == [
        ("apply", str(export / "labels.toml"), "--profile", "default", "owner/repo")
    ]


def test_apply_labels_defaults_to_current_directory(
    tmp_path, monkeypatch, captured_labelmaker
):
    """Omitting `labels_dir` keeps the historical CWD-relative behaviour."""
    monkeypatch.chdir(tmp_path)

    apply_labels(Config(), "owner/repo", is_awesome=False)

    assert captured_labelmaker[0][1] == "labels.toml"


def test_apply_labels_applies_awesome_profile_from_labels_dir(
    tmp_path, monkeypatch, captured_labelmaker
):
    """The awesome profile reads the same relocated `labels.toml`."""
    monkeypatch.chdir(tmp_path)
    export = tmp_path / "export"
    export.mkdir()

    apply_labels(Config(), "owner/awesome-list", is_awesome=True, labels_dir=export)

    profiles = [call[3] for call in captured_labelmaker]
    assert profiles == ["default", "awesome"]
    assert {call[1] for call in captured_labelmaker} == {str(export / "labels.toml")}


def test_apply_labels_merges_extra_label_directories(
    tmp_path, monkeypatch, captured_labelmaker
):
    """Committed and downloaded `extra-labels/` files are both applied."""
    monkeypatch.chdir(tmp_path)
    committed = tmp_path / "extra-labels"
    committed.mkdir()
    (committed / "hand-written.toml").write_text("", encoding="UTF-8")

    export = tmp_path / "export"
    (export / "extra-labels").mkdir(parents=True)
    (export / "extra-labels" / "downloaded.toml").write_text("", encoding="UTF-8")

    apply_labels(Config(), "owner/repo", is_awesome=False, labels_dir=export)

    applied = [call[1] for call in captured_labelmaker[1:]]
    # The committed directory stays CWD-relative, the download absolute. Both
    # are built from `Path`, as the production code does, so the expectation
    # carries the native separator on every platform.
    assert applied == [
        str(export / "extra-labels" / "downloaded.toml"),
        str(Path("extra-labels") / "hand-written.toml"),
    ]


def test_apply_labels_download_shadows_committed_file_of_same_name(
    tmp_path, monkeypatch, captured_labelmaker
):
    """A download wins over a committed file of the same name, applied once."""
    monkeypatch.chdir(tmp_path)
    committed = tmp_path / "extra-labels"
    committed.mkdir()
    (committed / "shared.toml").write_text("", encoding="UTF-8")

    export = tmp_path / "export"
    (export / "extra-labels").mkdir(parents=True)
    (export / "extra-labels" / "shared.toml").write_text("", encoding="UTF-8")

    apply_labels(Config(), "owner/repo", is_awesome=False, labels_dir=export)

    applied = [call[1] for call in captured_labelmaker[1:]]
    assert applied == [str(export / "extra-labels" / "shared.toml")]
