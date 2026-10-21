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

"""Tests for the `audit` command and the cooldown-bypass machinery behind it.

`audit` reports by default and upgrades with `--fix`. The
`exclude-newer-package` lifecycle lives in {mod}`repomatic.deps.uv`,
{mod}`repomatic.deps.lockfile` and {mod}`repomatic.deps.cooldown`.

The `repomatic.deps.report` renderers these helpers feed are covered by
`tests/test_dep_report.py`'s golden renders.
"""

from __future__ import annotations

import logging
import subprocess
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import tomlrt
from click.testing import CliRunner

from repomatic.cli import deps as cli_deps
from repomatic.cli.main import repomatic
from repomatic.config import Config
from repomatic.deps.audit import (
    AdvisorySource,
    AuditFix,
    VulnerablePackage,
)
from repomatic.deps.cooldown import (
    ExemptionKind,
    ExemptionValue,
    UvSettings,
    active_forecasts,
    compute_held_back_packages,
    pruned_forecasts,
    resolve_exclude_newer_cutoff,
    resolve_exclude_newer_span,
)
from repomatic.deps.lockfile import LockFile
from repomatic.deps.report import BYPASS_NEEDS_RELEASE, BypassForecast
from repomatic.deps.uv import UvError, relock, run_uv, uv_lock_command

REPO_ROOT = Path(__file__).parent.parent

NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)
"""A fixed instant for the tests whose expectations depend on the clock."""


def test_cooldown_windows_match_minimum_release_age() -> None:
    """The lock window and the install window are the same duration.

    `[tool.uv] exclude-newer` gates `uv lock`, while `[tool.repomatic]
    minimum-release-age` gates the `uvx` installs every workflow runs. A lock
    window wider than the install window resolves versions those installs then
    refuse, leaving a package pinned in `uv.lock` that CI cannot install.
    Keeping the two literals equal closes that band.
    """
    window = Config.minimum_release_age

    project = UvSettings.load(REPO_ROOT / "pyproject.toml").window
    assert project == window, (
        f"[tool.uv] exclude-newer is {project!r}, expected {window!r} to match "
        "[tool.repomatic] minimum-release-age."
    )

    bundled_path = REPO_ROOT / "repomatic" / "data" / "uv.toml"
    bundled = tomlrt.loads(bundled_path.read_text(encoding="UTF-8"))
    assert bundled.get("exclude-newer") == window, (
        f"{bundled_path.name} exclude-newer is "
        f"{bundled.get('exclude-newer')!r}, expected {window!r}."
    )


def _sample_vuln() -> VulnerablePackage:
    """Build a representative vulnerability advisory for the table tests."""
    return VulnerablePackage(
        name="aiohttp",
        current_version="3.14.0",
        advisory_id="GHSA-4fvr-rgm6-gqmc",
        advisory_title="Request smuggling",
        fixed_version="3.14.1",
        advisory_url="https://github.com/advisories/GHSA-4fvr-rgm6-gqmc",
        sources={AdvisorySource.UV_AUDIT},
    )


@pytest.fixture
def default_config(monkeypatch):
    """Pin the command to dataclass-default config, independent of the repo."""
    monkeypatch.setattr(cli_deps, "get_tool_config", lambda ctx: Config())


@pytest.fixture
def no_github_repo(monkeypatch):
    """Drop GITHUB_REPOSITORY so --repo defaults to empty (CI sets it)."""
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)


def _fail(*args, **kwargs):
    """Sentinel: a code path that must not run in the test under exercise."""
    raise AssertionError("unexpected call")


def test_audit_report_lists_vulnerabilities_and_exits_nonzero(
    monkeypatch, default_config, no_github_repo
):
    monkeypatch.setattr(
        cli_deps, "collect_vulnerable_packages", lambda *a, **k: [_sample_vuln()]
    )
    result = CliRunner().invoke(
        repomatic,
        ["--no-color", "--table-format", "github", "audit"],
        catch_exceptions=False,
    )
    assert result.exit_code == 1
    assert "aiohttp" in result.output
    assert "GHSA-4fvr-rgm6-gqmc" in result.output


def test_audit_report_clean_exits_zero(monkeypatch, default_config, no_github_repo):
    monkeypatch.setattr(cli_deps, "collect_vulnerable_packages", lambda *a, **k: [])
    result = CliRunner().invoke(
        repomatic, ["--no-color", "audit"], catch_exceptions=False
    )
    assert result.exit_code == 0
    assert "No known vulnerabilities found." in result.output


def test_audit_exit_zero_overrides_findings(
    monkeypatch, default_config, no_github_repo
):
    monkeypatch.setattr(
        cli_deps, "collect_vulnerable_packages", lambda *a, **k: [_sample_vuln()]
    )
    result = CliRunner().invoke(
        repomatic, ["--no-color", "audit", "--exit-zero"], catch_exceptions=False
    )
    assert result.exit_code == 0


def test_audit_report_does_not_call_the_fix_engine(
    monkeypatch, default_config, no_github_repo
):
    """Report mode is read-only: it must never reach the upgrade path."""
    monkeypatch.setattr(cli_deps, "collect_vulnerable_packages", lambda *a, **k: [])
    monkeypatch.setattr(cli_deps, "fix_vulnerable_deps", _fail)
    result = CliRunner().invoke(
        repomatic, ["--no-color", "audit"], catch_exceptions=False
    )
    assert result.exit_code == 0


def test_audit_output_writes_markdown(
    monkeypatch, default_config, no_github_repo, tmp_path
):
    monkeypatch.setattr(
        cli_deps, "collect_vulnerable_packages", lambda *a, **k: [_sample_vuln()]
    )
    out = tmp_path / "report.md"
    result = CliRunner().invoke(
        repomatic, ["--no-color", "audit", "--output", str(out)], catch_exceptions=False
    )
    assert result.exit_code == 1
    content = out.read_text(encoding="UTF-8")
    assert "## Vulnerabilities" in content
    assert "aiohttp" in content


def test_audit_passes_configured_sources_without_repo(
    monkeypatch, default_config, no_github_repo
):
    """Without a repository, the command still hands over every configured source.

    Skipping the GitHub Advisory Database is the library's call, made where
    the query would run, so the command passes the list through untouched.
    """
    captured = {}

    def fake_collect(lock_path, repo=None, sources=None):
        captured["repo"] = repo
        captured["sources"] = sources
        return []

    monkeypatch.setattr(cli_deps, "collect_vulnerable_packages", fake_collect)
    result = CliRunner().invoke(
        repomatic, ["--no-color", "audit"], catch_exceptions=False
    )
    assert result.exit_code == 0
    assert captured["repo"] is None
    assert captured["sources"] == [
        AdvisorySource.UV_AUDIT,
        AdvisorySource.GITHUB_ADVISORIES,
    ]


@pytest.mark.parametrize(
    ("args", "expected"),
    (
        pytest.param(["audit"], "No known vulnerabilities found.", id="report"),
        pytest.param(["audit", "--fix"], "No fixable vulnerabilities found.", id="fix"),
    ),
)
def test_audit_github_only_sources_without_repo_query_nothing(
    monkeypatch, no_github_repo, args, expected
):
    """A GitHub-only source list without a repository runs no `uv audit`.

    The one configured source cannot run without a repository, so the list the
    library acts on is empty. An empty list must not read as "every source",
    or the run falls back to the database the configuration left out.
    """
    config = Config()
    config.vulnerable_deps.sources = [AdvisorySource.GITHUB_ADVISORIES.value]
    monkeypatch.setattr(cli_deps, "get_tool_config", lambda ctx: config)
    monkeypatch.setattr("repomatic.deps.audit._run_uv_audit", _fail)
    monkeypatch.setattr("repomatic.deps.audit.fetch_dependabot_alerts", _fail)
    result = CliRunner().invoke(
        repomatic, ["--no-color", *args], catch_exceptions=False
    )
    assert result.exit_code == 0, result.output
    assert expected in result.output


def test_audit_keeps_github_source_with_repo(monkeypatch, default_config):
    captured = {}

    def fake_collect(lock_path, repo=None, sources=None):
        captured["repo"] = repo
        captured["sources"] = sources
        return []

    monkeypatch.setattr(cli_deps, "collect_vulnerable_packages", fake_collect)
    result = CliRunner().invoke(
        repomatic,
        ["--no-color", "audit", "--repo", "owner/name"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert captured["repo"] == "owner/name"
    assert AdvisorySource.GITHUB_ADVISORIES in captured["sources"]


def test_audit_fix_delegates_to_engine(
    monkeypatch, default_config, no_github_repo, tmp_path
):
    """The upgrade report reaches both the terminal and the `--output` file."""
    calls = {}

    def fake_fix(lock_path, repo=None, sources=None):
        calls["fixed"] = True
        return AuditFix(
            vulnerabilities=[_sample_vuln()],
            changes=[("aiohttp", "3.14.0", "3.14.1")],
        )

    monkeypatch.setattr(cli_deps, "fix_vulnerable_deps", fake_fix)
    monkeypatch.setattr(cli_deps, "collect_vulnerable_packages", _fail)
    out = tmp_path / "report.md"
    result = CliRunner().invoke(
        repomatic,
        ["--no-color", "audit", "--fix", "--output", str(out)],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert calls.get("fixed")
    assert "Upgraded vulnerable packages." in result.output
    report = out.read_text(encoding="UTF-8")
    assert "## Vulnerabilities" in report
    assert "`3.14.0` → `3.14.1`" in report
    assert report.strip() in result.output


def test_audit_fix_no_fixable_exits_zero(monkeypatch, default_config, no_github_repo):
    monkeypatch.setattr(cli_deps, "fix_vulnerable_deps", lambda *a, **k: AuditFix())
    result = CliRunner().invoke(
        repomatic, ["--no-color", "audit", "--fix"], catch_exceptions=False
    )
    assert result.exit_code == 0
    assert "No fixable vulnerabilities found." in result.output


def test_audit_fix_skipped_when_sync_disabled(tmp_path, monkeypatch, no_github_repo):
    """`vulnerable-deps.sync = false` in `pyproject.toml` turns the fix off."""
    (tmp_path / "pyproject.toml").write_text(
        "[tool.repomatic]\nvulnerable-deps.sync = false\n", encoding="UTF-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_deps, "fix_vulnerable_deps", _fail)
    result = CliRunner().invoke(
        repomatic, ["--no-color", "audit", "--fix"], catch_exceptions=False
    )
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Command builders and runner
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("extra", "window", "expected"),
    (
        pytest.param(
            (),
            "1 week",
            ["uv", "--no-progress", "lock", "--exclude-newer", "1 week"],
            id="window",
        ),
        pytest.param(
            ("--upgrade",),
            "",
            ["uv", "--no-progress", "lock", "--upgrade"],
            id="no-window",
        ),
    ),
)
def test_uv_lock_command(extra, window, expected):
    """The project's window rides after the extra arguments, when it has one."""
    assert uv_lock_command(*extra, exclude_newer=window) == expected


def test_run_uv_captures_output_and_carries_stderr():
    """A failure raises with the command's stderr, which it captured."""
    completed = SimpleNamespace(returncode=2, stdout="", stderr="No solution found")
    with (
        patch("repomatic.deps.uv.subprocess.run", return_value=completed) as run,
        pytest.raises(UvError, match="No solution found"),
    ):
        run_uv(["uv", "lock"], cwd=Path("/papaya"))
    assert run.call_args.kwargs["capture_output"] is True
    assert run.call_args.kwargs["encoding"] == "UTF-8"
    assert run.call_args.kwargs["cwd"] == Path("/papaya")


def test_run_uv_without_check_returns_the_failure():
    """`uv audit` exits non-zero when it finds something: the caller reads it."""
    completed = SimpleNamespace(returncode=1, stdout="{}", stderr="")
    with patch("repomatic.deps.uv.subprocess.run", return_value=completed):
        assert run_uv(["uv", "audit"], check=False) is completed


def test_relock_is_a_plain_lock():
    """A version bump refreshes the lock, never upgrades a dependency in it.

    No `--upgrade`, and no explicit window: the bump relocks with the project's
    own settings, exactly as the workflow's `uv lock` steps do.
    """
    completed = SimpleNamespace(returncode=0, stdout="", stderr="")
    with patch("repomatic.deps.uv.subprocess.run", return_value=completed) as run:
        relock()
    assert run.call_args.args[0] == ["uv", "--no-progress", "lock"]


# ---------------------------------------------------------------------------
# Window and entry parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "span"),
    (
        # A zero span is falsy: it must still read as a span, not as nothing.
        ("0 days", timedelta(0)),
        ("0 day", timedelta(0)),
        ("P0D", timedelta(0)),
        ("1 week", timedelta(days=7)),
        ("P7D", timedelta(days=7)),
        ("2026-03-18T16:39:02Z", None),
        ("", None),
    ),
)
def test_resolve_exclude_newer_span(value, span):
    assert resolve_exclude_newer_span(value) == span


@pytest.mark.parametrize(
    ("value", "cutoff"),
    (
        ("0 days", NOW),
        ("P0D", NOW),
        ("1 week", NOW - timedelta(days=7)),
        ("2026-03-18T16:39:02Z", datetime(2026, 3, 18, 16, 39, 2, tzinfo=UTC)),
        ("", None),
        ("someday", None),
    ),
)
def test_resolve_exclude_newer_cutoff(value, cutoff):
    assert resolve_exclude_newer_cutoff(value, NOW) == cutoff


@pytest.mark.parametrize(
    ("raw", "kind", "tracks_latest", "fixed"),
    (
        pytest.param(False, ExemptionKind.LIFT, True, None, id="false-lift"),
        pytest.param("0 day", ExemptionKind.SPAN, True, None, id="zero-span"),
        pytest.param("P7D", ExemptionKind.SPAN, True, None, id="iso-span"),
        # A bare date names an instant per locking-machine timezone: not
        # trusted as a cutoff until the freeze pass pins it.
        pytest.param("2026-06-13", ExemptionKind.DATE, False, None, id="bare-date"),
        pytest.param(
            "2026-06-14T00:00:00Z",
            ExemptionKind.TIMESTAMP,
            False,
            datetime(2026, 6, 14, tzinfo=UTC),
            id="timestamp",
        ),
        pytest.param("False", None, False, None, id="stringified-false"),
        pytest.param("someday", None, False, None, id="unparsable"),
    ),
)
def test_exemption_value_reads_every_form(raw, kind, tracks_latest, fixed):
    value = ExemptionValue.parse(raw)
    assert value.kind is kind
    assert value.tracks_latest is tracks_latest
    assert value.fixed_cutoff == fixed


def test_exemption_value_pins_a_bare_date_to_utc():
    """uv expands a bare date to the next UTC midnight: the pin writes that."""
    assert ExemptionValue.parse("2026-06-13").pinned == "2026-06-14T00:00:00Z"
    assert ExemptionValue.parse("2026-06-14T00:00:00Z").pinned is None


@pytest.mark.parametrize(
    ("raw", "released", "admitted"),
    (
        pytest.param(False, date(2026, 10, 20), True, id="lift-admits-today"),
        pytest.param("0 day", date(2026, 10, 19), True, id="zero-span"),
        pytest.param("P7D", date(2026, 10, 1), True, id="span-aged"),
        pytest.param("P7D", date(2026, 10, 18), False, id="span-fresh"),
        pytest.param("2026-10-10", date(2026, 10, 10), True, id="date-same-day"),
        pytest.param("2026-10-10", date(2026, 10, 11), False, id="date-after"),
        pytest.param(
            "2026-10-11T00:00:00Z", date(2026, 10, 10), True, id="timestamp-end-of-day"
        ),
        # A cutoff inside the release day cannot vouch for every upload of it.
        pytest.param(
            "2026-10-10T12:00:00Z", date(2026, 10, 10), False, id="timestamp-mid-day"
        ),
        pytest.param("someday", date(2020, 1, 1), False, id="unparsable"),
    ),
)
def test_exemption_value_admits(raw, released, admitted):
    assert ExemptionValue.parse(raw).admits(released, NOW) is admitted


# ---------------------------------------------------------------------------
# The [tool.uv] lifecycle
# ---------------------------------------------------------------------------


def _write_uv_config(tmp_path: Path, uv_lines: str) -> Path:
    """Write a minimal `pyproject.toml` with the given `[tool.uv]` body."""
    path = tmp_path / "pyproject.toml"
    path.write_text(f"[tool.uv]\n{uv_lines}", encoding="UTF-8")
    return path


def _write_lock(tmp_path: Path, *packages: tuple[str, str, str]) -> Path:
    """Write a minimal `uv.lock` with a `P1W` cooldown span.

    Each package is a `(name, version, upload_time)` triple; an empty upload
    time omits the `sdist` block, mimicking a git or path source.
    """
    lines = [
        "version = 1",
        'requires-python = ">=3.10"',
        "",
        "[options]",
        'exclude-newer = "0001-01-01T00:00:00Z"',
        'exclude-newer-span = "P1W"',
    ]
    for name, version, upload in packages:
        lines.extend([
            "",
            "[[package]]",
            f'name = "{name}"',
            f'version = "{version}"',
            'source = { registry = "https://pypi.org/simple" }',
        ])
        if upload:
            lines.append(
                f'sdist = {{ url = "https://example.test/{name}.tar.gz",'
                f' upload-time = "{upload}" }}'
            )
    path = tmp_path / "uv.lock"
    path.write_text("\n".join(lines) + "\n", encoding="UTF-8")
    return path


def _stamp(instant: datetime) -> str:
    return instant.strftime("%Y-%m-%dT%H:%M:%SZ")


def _prune(pyproject: Path, lock: Path, now: datetime | None = None) -> set[str]:
    """Run the prune pass, save the file, and return the pruned names."""
    settings = UvSettings.load(pyproject)
    pruned = settings.prune(LockFile.load(lock), now or datetime.now(UTC))
    settings.save()
    return set(pruned)


def _freeze(pyproject: Path, lock: Path) -> set[str]:
    """Run the freeze pass, save the file, and return the rewritten names."""
    settings = UvSettings.load(pyproject)
    frozen = settings.freeze(LockFile.load(lock))
    settings.save()
    return frozen


def test_prune_returns_pruned_names(tmp_path):
    """An entry whose held version aged past the cutoff is dropped by name."""
    pyproject = _write_uv_config(
        tmp_path,
        'exclude-newer = "1 week"\n'
        'exclude-newer-package = { mango = "2026-01-02T00:00:00Z" }\n',
    )
    lock = _write_lock(tmp_path, ("mango", "2.0.0", "2026-01-01T12:00:00Z"))
    assert _prune(pyproject, lock) == {"mango"}
    assert "exclude-newer-package" not in pyproject.read_text(encoding="UTF-8")


def test_prune_keeps_active_freeze(tmp_path):
    """A freeze whose held version is still inside the window stays put."""
    fresh = _stamp(datetime.now(UTC) - timedelta(days=1))
    pyproject = _write_uv_config(
        tmp_path,
        f'exclude-newer = "1 week"\nexclude-newer-package = {{ mango = "{fresh}" }}\n',
    )
    lock = _write_lock(tmp_path, ("mango", "2.0.0", fresh))
    before = pyproject.read_text(encoding="UTF-8")
    assert _prune(pyproject, lock) == set()
    assert pyproject.read_text(encoding="UTF-8") == before


@pytest.mark.parametrize("window", ("0 days", "P0D"))
def test_prune_under_a_zero_window(tmp_path, window, caplog):
    """A disabled cooldown prunes every entry its lock ages, without a warning.

    `0 days` once parsed to no window at all: the pass warned it could not
    parse the value and pruned nothing, on every run.
    """
    pyproject = _write_uv_config(
        tmp_path,
        f'exclude-newer = "{window}"\n'
        'exclude-newer-package = { mango = "2026-10-12T00:00:00Z" }\n',
    )
    lock = _write_lock(tmp_path, ("mango", "2.0.0", "2026-10-10T12:00:00Z"))
    with caplog.at_level(logging.WARNING):
        assert _prune(pyproject, lock, NOW) == {"mango"}
    assert "Cannot parse" not in caplog.text


YESTERDAY = "yesterday"
"""Stands for a cutoff one day old, resolved in the test body.

A timestamp computed at collection differs between xdist workers.
"""


@pytest.mark.parametrize(
    ("entry", "packages", "pruned"),
    [
        pytest.param(
            '"2026-01-02T00:00:00Z"', (), {"quince"}, id="aged-cutoff-not-locked"
        ),
        pytest.param(YESTERDAY, (), set(), id="fresh-cutoff-not-locked"),
        pytest.param('"0 day"', (), set(), id="span-not-locked"),
        pytest.param("false", (), set(), id="lift-not-locked"),
        # A bare date names a different instant on each machine: the freeze
        # pass pins it to UTC first, and a later run prunes the pinned form.
        pytest.param('"2026-01-02"', (), set(), id="bare-date-not-locked"),
        # The lock holds the package from a git or path source: its entry
        # waits for a release, whatever the age of the cutoff.
        pytest.param(
            '"2026-01-02T00:00:00Z"',
            (("quince", "1.0.0.dev0", ""),),
            set(),
            id="aged-cutoff-locked-unreleased",
        ),
    ],
)
def test_prune_ages_an_entry_the_lock_does_not_hold_by_its_cutoff(
    tmp_path, entry, packages, pruned
):
    """A package outside the lock has no held version, so its cutoff ages.

    Once the rolling cutoff has passed a fixed timestamp, the entry admits no
    release the cooldown still withholds, and it blocks every later one.
    """
    if entry == YESTERDAY:
        entry = f'"{_stamp(datetime.now(UTC) - timedelta(days=1))}"'
    pyproject = _write_uv_config(
        tmp_path,
        f'exclude-newer = "1 week"\nexclude-newer-package = {{ quince = {entry} }}\n',
    )
    lock = _write_lock(tmp_path, ("mango", "2.0.0", "2026-01-01T12:00:00Z"), *packages)
    before = pyproject.read_text(encoding="UTF-8")
    assert _prune(pyproject, lock) == pruned
    if not pruned:
        assert pyproject.read_text(encoding="UTF-8") == before


def test_prune_matches_a_lock_name_in_any_spelling(tmp_path):
    """An entry keyed by another spelling of the name still finds its release."""
    pyproject = _write_uv_config(
        tmp_path,
        'exclude-newer = "1 week"\n'
        'exclude-newer-package = { Lemon_Curd = "2026-01-03T00:00:00Z" }\n',
    )
    lock = _write_lock(tmp_path, ("lemon-curd", "2.0.0", "2026-01-01T12:00:00Z"))
    assert _prune(pyproject, lock) == {"Lemon_Curd"}


def test_prune_leaves_a_comment_naming_the_pruned_entry(tmp_path):
    """A partial prune keeps the table's comment, which still names the entry."""
    fresh = _stamp(datetime.now(UTC) - timedelta(days=1))
    pyproject = _write_uv_config(
        tmp_path,
        'exclude-newer = "1 week"\n'
        "# mango keeps a permanent span. papaya carries a frozen cutoff,\n"
        "# which the next prune removes.\n"
        f'exclude-newer-package = {{ mango = "0 day", papaya = "{fresh}" }}\n',
    )
    lock = _write_lock(
        tmp_path,
        ("mango", "2.0.0", ""),
        ("papaya", "1.0.0", "2026-01-01T12:00:00Z"),
    )
    settings = UvSettings.load(pyproject)
    pruned = settings.prune(LockFile.load(lock), datetime.now(UTC))
    assert set(pruned) == {"papaya"}
    assert settings.comment_mentions(pruned) == ["papaya"]


@pytest.mark.parametrize(
    ("uv_lines", "names", "expected"),
    [
        pytest.param(
            "# lemon_curd is held for a fix.\n"
            'exclude-newer-package = { mango = "0 day" }\n',
            {"lemon-curd"},
            ["lemon-curd"],
            id="separator-spelling-ignored",
        ),
        pytest.param(
            'exclude-newer-package = { mango = "0 day" }  # Lemon-Curd too.\n',
            {"lemon-curd"},
            ["lemon-curd"],
            id="end-of-line-comment",
        ),
        pytest.param(
            "# lemon-curd is held for a fix.\n"
            'exclude-newer-package = { mango = "0 day" }\n',
            {"lemon"},
            [],
            id="prefix-of-another-name",
        ),
        pytest.param(
            "# mango keeps a permanent span.\n"
            'exclude-newer-package = { mango = "0 day" }\n',
            {"papaya"},
            [],
            id="not-mentioned",
        ),
        pytest.param(
            "# papaya was the last entry.\n",
            {"papaya"},
            [],
            id="table-gone",
        ),
    ],
)
def test_comment_mentions(tmp_path, uv_lines, names, expected):
    """Only the comments attached to `exclude-newer-package` are searched."""
    pyproject = _write_uv_config(tmp_path, f'exclude-newer = "1 week"\n{uv_lines}')
    assert UvSettings.load(pyproject).comment_mentions(names) == expected


def test_freeze_returns_frozen_names(tmp_path):
    """A relative-span bypass is rewritten to hold the locked version."""
    pyproject = _write_uv_config(
        tmp_path,
        'exclude-newer = "1 week"\nexclude-newer-package = { mango = "0 day" }\n',
    )
    lock = _write_lock(tmp_path, ("mango", "2.0.0", "2026-07-01T12:00:00Z"))
    assert _freeze(pyproject, lock) == {"mango"}
    # Frozen just past the held upload: the day after (upload + 1 day).
    assert (
        'exclude-newer-package = { mango = "2026-07-03T00:00:00Z" }'
        in pyproject.read_text(encoding="UTF-8")
    )
    # Re-running is a no-op: the frozen cutoff already holds.
    assert _freeze(pyproject, lock) == set()


def test_freeze_reads_a_false_lift_as_a_zero_span(tmp_path):
    """uv's `false` lifts the cooldown the way a zero span does, so it freezes.

    The entry once read as the string `False`: unparsable, so the freeze left
    it tracking the latest release, and the forecast gave it an expiry.
    """
    pyproject = _write_uv_config(
        tmp_path,
        'exclude-newer = "1 week"\nexclude-newer-package = { mango = false }\n',
    )
    lock = _write_lock(tmp_path, ("mango", "2.0.0", "2026-07-01T12:00:00Z"))
    assert (
        active_forecasts(UvSettings.load(pyproject).entries(), LockFile.load(lock), NOW)
        == []
    )
    assert _freeze(pyproject, lock) == {"mango"}
    assert '{ mango = "2026-07-03T00:00:00Z" }' in pyproject.read_text(encoding="UTF-8")


def test_freeze_pins_bare_date(tmp_path):
    """A legacy bare date is pinned to the equivalent explicit UTC instant."""
    pyproject = _write_uv_config(
        tmp_path,
        'exclude-newer = "1 week"\nexclude-newer-package = { mango = "2026-06-13" }\n',
    )
    lock = _write_lock(tmp_path, ("mango", "2.0.0", "2026-06-12T08:00:00Z"))
    assert _freeze(pyproject, lock) == {"mango"}
    assert '"2026-06-14T00:00:00Z"' in pyproject.read_text(encoding="UTF-8")


def test_freeze_keeps_span_without_upload_time(tmp_path):
    """A git/path package has no release to freeze against: span kept as-is."""
    pyproject = _write_uv_config(
        tmp_path,
        'exclude-newer = "1 week"\nexclude-newer-package = { papaya = "0 day" }\n',
    )
    lock = _write_lock(tmp_path, ("papaya", "1.0.0", ""))
    before = pyproject.read_text(encoding="UTF-8")
    assert _freeze(pyproject, lock) == set()
    assert pyproject.read_text(encoding="UTF-8") == before


def test_freeze_window_absorbs_same_day_patch(tmp_path):
    """A freeze holds a day-granular window, not a single version.

    The cutoff rounds up to the second UTC midnight after the held version's
    upload, so a patch released later the same day (or on the next calendar
    day) stays inside the window and is adopted on the next lock. This is the
    accepted trade-off documented on `freeze_cutoff_after`: pinning the cutoff
    to the exact upload instant is the only way to reject a same-day patch, so
    locking the window width here makes any such tightening a conscious change.
    """
    pyproject = _write_uv_config(
        tmp_path,
        'exclude-newer = "1 week"\nexclude-newer-package = { mango = "0 day" }\n',
    )
    # Held version shipped mid-afternoon; a patch could land hours later, same day.
    lock = _write_lock(tmp_path, ("mango", "2.0.0", "2026-07-27T15:58:06Z"))
    _freeze(pyproject, lock)
    assert (
        'exclude-newer-package = { mango = "2026-07-29T00:00:00Z" }'
        in pyproject.read_text(encoding="UTF-8")
    )
    # A same-day patch and a next-day release both fall before the cutoff, so uv
    # resolves up to them; a release two days on is excluded (window is bounded).
    cutoff = datetime(2026, 7, 29, tzinfo=UTC)
    assert datetime(2026, 7, 27, 20, 15, tzinfo=UTC) < cutoff
    assert datetime(2026, 7, 28, 23, 0, tzinfo=UTC) < cutoff
    assert datetime(2026, 7, 29, 9, 0, tzinfo=UTC) >= cutoff


LOCK_NAME_SPELLINGS = ("lemon-curd", "lemon_curd", "Lemon_Curd", "LEMON-CURD")
"""Spellings of one package name under PEP 503. The first is the one uv locks."""


@pytest.mark.parametrize("key", LOCK_NAME_SPELLINGS)
def test_freeze_matches_a_lock_name_in_any_spelling(tmp_path, key):
    """A span keyed by another spelling of the name is frozen like any other.

    A lookup by the raw key finds no upload time, reads the package as a git or
    path source, and leaves the span to track the latest release for good. The
    entry keeps the key the project wrote.
    """
    pyproject = _write_uv_config(
        tmp_path,
        f'exclude-newer = "1 week"\nexclude-newer-package = {{ {key} = "0 day" }}\n',
    )
    lock = _write_lock(tmp_path, ("lemon-curd", "2.0.0", "2026-07-01T12:00:00Z"))
    assert _freeze(pyproject, lock) == {key}
    assert (
        f'exclude-newer-package = {{ {key} = "2026-07-03T00:00:00Z" }}'
        in pyproject.read_text(encoding="UTF-8")
    )


@pytest.mark.parametrize("name", LOCK_NAME_SPELLINGS)
def test_exempt_freezes_a_fresh_release_in_any_spelling(tmp_path, name):
    """A package exempted under another spelling of its name gets a freeze.

    A lookup by the raw name finds no upload time, and the entry falls back to
    the permanent `"0 day"` span of a git or path source.
    """
    pyproject = _write_uv_config(tmp_path, 'exclude-newer = "1 week"\n')
    lock = _write_lock(tmp_path, ("lemon-curd", "2.0.0", "2026-10-18T12:00:00Z"))
    settings = UvSettings.load(pyproject)
    assert settings.exempt({name}, LockFile.load(lock), NOW) == {name}
    assert settings.save()
    assert (
        f'exclude-newer-package = {{ {name} = "2026-10-20T00:00:00Z" }}'
        in pyproject.read_text(encoding="UTF-8")
    )


@pytest.mark.parametrize("name", LOCK_NAME_SPELLINGS)
def test_exempt_skips_an_aged_release_in_any_spelling(tmp_path, name):
    """A release that cleared the window needs no exemption in any spelling.

    A lookup by the raw name finds no upload time, and the package is exempted
    like a git or path source.
    """
    pyproject = _write_uv_config(tmp_path, 'exclude-newer = "1 week"\n')
    lock = _write_lock(tmp_path, ("lemon-curd", "2.0.0", "2026-01-01T12:00:00Z"))
    settings = UvSettings.load(pyproject)
    assert settings.exempt({name}, LockFile.load(lock), NOW) == set()
    assert not settings.save()


@pytest.mark.parametrize("window", ("0 days", "P0D"))
def test_exempt_nothing_under_a_zero_window(tmp_path, window):
    """With the cooldown disabled, no fix needs an exemption to resolve.

    `0 days` once parsed to no window at all, which exempted every upgrade.
    """
    pyproject = _write_uv_config(tmp_path, f'exclude-newer = "{window}"\n')
    lock = _write_lock(tmp_path, ("mango", "2.0.0", "2026-10-20T08:00:00Z"))
    settings = UvSettings.load(pyproject)
    assert settings.exempt({"mango"}, LockFile.load(lock), NOW) == set()


def test_settings_save_writes_only_a_change(tmp_path):
    """Loading and saving untouched settings leaves the file alone."""
    pyproject = _write_uv_config(tmp_path, 'exclude-newer = "1 week"\n')
    settings = UvSettings.load(pyproject)
    assert not settings.save()
    assert settings.upsert({"mango": "2026-10-22T00:00:00Z"})
    assert settings.save()
    assert not settings.save()


def test_settings_never_create_a_missing_file(tmp_path):
    """A project with no `pyproject.toml` has no table to edit."""
    settings = UvSettings.load(tmp_path / "pyproject.toml")
    assert settings.window == ""
    assert settings.entries() == {}
    assert not settings.upsert({"mango": "0 day"})
    assert not settings.save()
    assert not (tmp_path / "pyproject.toml").exists()


# ---------------------------------------------------------------------------
# Forecasts
# ---------------------------------------------------------------------------


def test_active_forecasts_report_freezes_only(tmp_path):
    """Fixed-timestamp entries get an expiry; spans do not."""
    pyproject = _write_uv_config(
        tmp_path,
        'exclude-newer = "1 week"\n'
        "exclude-newer-package = {"
        ' cherry = "2026-01-02T00:00:00Z",'
        ' mango = "2026-07-02T00:00:00Z",'
        ' papaya = "0 day" }\n',
    )
    lock = _write_lock(tmp_path, ("mango", "2.0.0", "2026-07-01T12:00:00Z"))
    forecasts = active_forecasts(
        UvSettings.load(pyproject).entries(), LockFile.load(lock), NOW
    )
    # papaya is a permanent span: it has no row.
    assert [forecast.name for forecast in forecasts] == ["cherry", "mango"]
    # cherry left the lock, so it holds no version. Its own cutoff (2026-01-02)
    # plus the lock's P1W span dates it.
    assert forecasts[0].held_version == ""
    assert forecasts[0].expires.startswith("2026-01-09")
    assert forecasts[1].held_version == "2.0.0"
    # The held upload (2026-07-01) plus the lock's P1W span.
    assert forecasts[1].expires.startswith("2026-07-08")


def test_active_forecasts_flag_unreleased_hold(tmp_path):
    """A freeze holding a version with no upload time can only end by release."""
    pyproject = _write_uv_config(
        tmp_path,
        'exclude-newer = "1 week"\n'
        'exclude-newer-package = { papaya = "2026-07-02T00:00:00Z" }\n',
    )
    lock = _write_lock(tmp_path, ("papaya", "1.0.0.dev0", ""))
    forecasts = active_forecasts(
        UvSettings.load(pyproject).entries(), LockFile.load(lock), NOW
    )
    assert forecasts == [BypassForecast("papaya", "1.0.0.dev0", BYPASS_NEEDS_RELEASE)]
    assert forecasts[0].awaits_release


def test_pruned_forecasts_snapshot_cleared_freezes(tmp_path):
    """Pruned entries keep the version and the (past) date the freeze aged out."""
    lock = LockFile.load(
        _write_lock(tmp_path, ("mango", "2.0.0", "2026-01-01T12:00:00Z"))
    )
    records = pruned_forecasts(
        {"mango": ExemptionValue.parse("2026-01-03T00:00:00Z")}, lock, NOW
    )
    assert len(records) == 1
    assert records[0].name == "mango"
    assert records[0].held_version == "2.0.0"
    # The held upload (2026-01-01) plus the lock's P1W span, long past.
    assert records[0].expires.startswith("2026-01-08")
    assert pruned_forecasts({}, lock, NOW) == []


def test_pruned_forecasts_date_an_unlocked_entry_by_its_cutoff(tmp_path):
    """A cleared entry of a package outside the lock keeps its cutoff's expiry."""
    lock = LockFile.load(
        _write_lock(tmp_path, ("mango", "2.0.0", "2026-01-01T12:00:00Z"))
    )
    records = pruned_forecasts(
        {"quince": ExemptionValue.parse("2026-01-02T00:00:00Z")}, lock, NOW
    )
    assert len(records) == 1
    assert records[0].name == "quince"
    assert records[0].held_version == ""
    # The cutoff (2026-01-02) plus the lock's P1W span.
    assert records[0].expires.startswith("2026-01-09")


# ---------------------------------------------------------------------------
# Held-back probe
# ---------------------------------------------------------------------------


def test_held_back_probe_captures_uv_and_restores_the_lock(tmp_path):
    """The probe runs under a progress trail: uv's output must not stream.

    The probe's own `uv lock` once printed its resolution summary straight
    onto the trail's spinner row.
    """
    lock = _write_lock(tmp_path, ("mango", "2.0.0", "2026-10-01T12:00:00Z"))
    original = lock.read_text(encoding="UTF-8")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        # A different size, so the parse cache cannot mistake the new lock
        # for the old one on a filesystem with coarse timestamps.
        lock.write_text(
            original.replace('version = "2.0.0"', 'version = "2.10.0"').replace(
                "2026-10-01T12:00:00Z", "2026-10-19T12:00:00Z"
            ),
            encoding="UTF-8",
        )
        return SimpleNamespace(returncode=0, stdout="Resolved 1 package", stderr="")

    with patch("repomatic.deps.uv.subprocess.run", side_effect=fake_run):
        held = compute_held_back_packages(lock, LockFile.load(lock), {"mango"})

    ((cmd, kwargs),) = calls
    assert kwargs["capture_output"] is True
    assert "--exclude-newer-package" in cmd
    assert [(pkg.name, pkg.locked_version, pkg.available_version) for pkg in held] == [
        ("mango", "2.0.0", "2.10.0")
    ]
    assert lock.read_text(encoding="UTF-8") == original


def test_held_back_probe_failure_reports_nothing(tmp_path):
    """A probe that cannot resolve skips the section and keeps the lock."""
    lock = _write_lock(tmp_path, ("mango", "2.0.0", "2026-10-01T12:00:00Z"))
    original = lock.read_text(encoding="UTF-8")
    failure = SimpleNamespace(returncode=1, stdout="", stderr="No solution found")
    with patch("repomatic.deps.uv.subprocess.run", return_value=failure):
        assert compute_held_back_packages(lock, LockFile.load(lock), ()) == []
    assert lock.read_text(encoding="UTF-8") == original


def test_uv_error_is_a_called_process_error():
    """Callers catching the standard error keep working."""
    assert issubclass(UvError, subprocess.CalledProcessError)


# ---------------------------------------------------------------------------
# Lock views
# ---------------------------------------------------------------------------


def test_lock_specifiers_isolate_unconditional_declarations() -> None:
    """`by_main` holds only what a plain install of the project pulls in.

    A version marker leaves a dependency unconditional; an extra marker moves
    it to that extra's box wherever it sits in the expression; a dev group
    never lands there at all.
    """
    lock_data = {
        "package": [
            {
                "name": "my-project",
                "metadata": {
                    "requires-dist": [
                        {"name": "click", "specifier": ">=8.0"},
                        {
                            "name": "tomli",
                            "specifier": ">=2",
                            "marker": "python_full_version < '3.11'",
                        },
                        {
                            "name": "sphinx",
                            "specifier": ">=8",
                            "marker": "extra == 'sphinx'",
                        },
                        {
                            "name": "rich",
                            "specifier": ">=12.6",
                            "marker": (
                                "python_full_version >= '3.11' "
                                "and extra == 'screenshot'"
                            ),
                        },
                    ],
                    "requires-dev": {
                        "test": [{"name": "requests", "specifier": ">=2.34"}],
                    },
                },
            },
            {"name": "sphinx", "dependencies": [{"name": "requests"}]},
        ],
    }

    specs = LockFile(lock_data).specifiers

    assert specs.by_main["my-project"] == {"click": ">=8.0", "tomli": ">=2"}
    assert specs.by_subgraph["sphinx"] == {"sphinx": ">=8"}
    assert specs.by_subgraph["screenshot"] == {"rich": ">=12.6"}
    assert specs.by_subgraph["test"] == {"requests": ">=2.34"}
    # Edge labels still need every declaration, dev groups included.
    assert specs.by_package["my-project"]["requests"] == ">=2.34"
    # A package the lockfile carries no metadata for is absent, not empty.
    assert "sphinx" not in specs.by_main


def test_lock_views_of_a_missing_file(tmp_path):
    """A lock that does not exist yet reads as an empty one."""
    lock = LockFile.load(tmp_path / "uv.lock")
    assert lock.versions == {}
    assert lock.upload_times == {}
    assert lock.exclude_newer == ""
    assert lock.cooldown_span is None
    assert lock.version_of("mango") == ""
    assert lock.uploaded_at("mango") is None
