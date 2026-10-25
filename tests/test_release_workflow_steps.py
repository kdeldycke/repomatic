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

"""Run the exact command of each release step against a mocked GitHub.

A step gated on a release commit runs on no ordinary push, so no CI run between
two releases reaches it: a wrong argument would only surface as a failed
release. Each test here reads the step's `run:` line out of the workflow file,
expands its environment variables with plausible values, and invokes the
command that line holds. The arguments a release passes are the ones these
tests run.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from click_extra.testing import CliRunner

from repomatic.cli.main import repomatic
from repomatic.github.gh import GhCommandError
from repomatic.github.pr import PrOperation, PrSyncResult
from repomatic.github.pr_body import PrContent
from repomatic.github.threads import BOT_ISSUE_LABEL
from repomatic.metadata.core import Metadata
from repomatic.upstream import DEFAULT_REPO
from tests.conftest import load_workflow, run_git, seed_commit, write_bundle

REPO_ROOT = Path(__file__).parent.parent

REPOSITORY = "kdeldycke/papaya"
"""The repository the steps run in."""

VERSION = "1.2.3"
"""The version the release publishes."""

COMMIT = "6f27db47612aaee06fdf08744b09a9f5f6c26a1b"
"""The release commit."""

CLI_MARK = "-- repomatic "
"""What separates the command that starts the CLI from the arguments it gets."""

VARIABLE_RE = re.compile(r"\$\{(\w+)\}|\$(\w+)")
"""A shell variable, braced or bare."""

PYPROJECT = f"""\
[project]
name = "papaya"
version = "{VERSION}"

[tool.repomatic]
release-assets = ["papaya.zip", "mango.zip"]
"""
"""The project the release steps work on."""

CHANGELOG = f"""\
# Changelog

## [`{VERSION}` (2026-10-21)](https://github.com/{REPOSITORY}/compare/v1.2.2...v{VERSION})

- Add a juicer.

## [`1.2.2` (2026-10-01)](https://github.com/{REPOSITORY}/compare/v1.2.1...v1.2.2)

- Peel faster.
"""
"""A changelog holding the section of the release."""


def find_step(document: dict, job: str, name: str) -> dict:
    """The step of *job* named *name*, the one step matching it."""
    steps: list[dict] = [
        step for step in document["jobs"][job]["steps"] if step.get("name") == name
    ]
    assert len(steps) == 1, f"{job} has {len(steps)} step(s) named {name!r}"
    return steps[0]


def step_argv(step: dict, env: dict[str, str]) -> list[str]:
    """The arguments a step passes to the CLI, its variables expanded.

    Every variable the line reads must be one the step declares in its `env:`
    or one GitHub sets, which *env* stands in for: an undeclared one would
    reach the CLI empty.

    :param step: The workflow step.
    :param env: The values of the variables the line reads.
    :return: The arguments after the `repomatic` command.
    """
    run = " ".join(step["run"].split())
    assert CLI_MARK in run, f"No repomatic call in {run!r}"
    arguments = run.split(CLI_MARK, 1)[1]
    assert "${{" not in arguments, f"Expression left in the run line: {run!r}"

    def expand(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        assert name in (step.get("env") or {}) or name.startswith("GITHUB_"), (
            f"{name} is neither in the step's env nor set by GitHub"
        )
        return env[name]

    return [VARIABLE_RE.sub(expand, token) for token in shlex.split(arguments)]


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A checkout of the project being released, the working directory."""
    (tmp_path / "pyproject.toml").write_text(PYPROJECT, encoding="UTF-8")
    (tmp_path / "changelog.md").write_text(CHANGELOG, encoding="UTF-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_REPOSITORY", REPOSITORY)
    monkeypatch.setenv("GITHUB_ACTOR", "kevin")
    return tmp_path


def invoke(argv: list[str]):
    """Run the CLI on *argv*, as the step would."""
    return CliRunner().invoke(repomatic, argv)


# ---------------------------------------------------------------------------
# _release-build.yaml
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "reported"),
    (
        pytest.param(f"Release `v{VERSION}` (#42)", True, id="squashed"),
        pytest.param(f"[changelog] Release v{VERSION}", False, id="rebased"),
    ),
)
def test_squashed_release_step(project, monkeypatch, message, reported):
    """A squash-merged release PR reopens one issue and fails the job."""
    event = project / "event.json"
    event.write_text(
        json.dumps({"head_commit": {"message": message}}), encoding="UTF-8"
    )
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    step = find_step(
        load_workflow("_release-build.yaml"),
        "squashed-release",
        "Open or update the squash-merge issue, then fail",
    )

    with patch(
        "repomatic.cli.release.manage_issue_lifecycle", return_value=7
    ) as lifecycle:
        result = invoke(step_argv(step, {}))

    if not reported:
        assert result.exit_code == 0, result.output
        lifecycle.assert_not_called()
        return
    assert result.exit_code == 1, result.output
    kwargs = lifecycle.call_args.kwargs
    assert kwargs["has_issues"] is True
    assert kwargs["title"] == "🚨 Squashed release"
    assert kwargs["labels"] == (BOT_ISSUE_LABEL,)
    assert "#42" in kwargs["body"]
    assert kwargs["repository"] == REPOSITORY
    assert kwargs["assignees"] == ("kevin",)
    assert (
        "::error::Squash merge detected. Release was skipped. "
        f"See https://github.com/{REPOSITORY}/issues/7"
    ) in result.output


# ---------------------------------------------------------------------------
# _release-engine.yaml
# ---------------------------------------------------------------------------


ENGINE = load_workflow("_release-engine.yaml")


def test_create_release_step(project):
    """The draft gets the release notes, the target commit and the package."""
    artifact = project / "release_artifact"
    artifact.mkdir()
    wheel = artifact / f"papaya-{VERSION}-py3-none-any.whl"
    wheel.touch()
    step = find_step(ENGINE, "create-release", "Create GitHub release draft")
    argv = step_argv(step, {"CURRENT_VERSION": VERSION, "COMMIT": COMMIT})

    with patch(
        "repomatic.github.releases.run_gh_command",
        side_effect=[GhCommandError("release not found"), ""],
    ) as mock_gh:
        result = invoke(argv)

    assert result.exit_code == 0, result.output
    create = mock_gh.call_args.args[0]
    assert create[:8] == [
        "release",
        "create",
        f"v{VERSION}",
        "--draft",
        "--target",
        COMMIT,
        "--title",
        f"v{VERSION}",
    ]
    notes = create[create.index("--notes") + 1]
    assert "- Add a juicer." in notes
    assert f"https://pypi.org/project/papaya/{VERSION}/" in notes
    assert create[-1] == str(wheel)


def test_create_release_step_skips_an_existing_release(project):
    """A re-run of the job finds the draft its first attempt created."""
    step = find_step(ENGINE, "create-release", "Create GitHub release draft")
    argv = step_argv(step, {"CURRENT_VERSION": VERSION, "COMMIT": COMMIT})
    with patch(
        "repomatic.github.releases.run_gh_command", return_value='{"tagName": "v1"}'
    ) as mock_gh:
        result = invoke(argv)
    assert result.exit_code == 0, result.output
    assert mock_gh.call_count == 1


def test_compile_binaries_attestation_step(project):
    """The binary's provenance is checked, and its bundle named after it."""
    binary = project / f"papaya-{VERSION}-linux-x64.bin"
    binary.write_bytes(b"\x7fELF")
    bundle = write_bundle(project / "attestation.json", binary.name)
    step = find_step(
        ENGINE,
        "compile-binaries",
        "Verify the attestation and name its bundle after the binary",
    )

    with patch("repomatic.release.attestation.run_gh_command") as mock_gh:
        result = invoke(step_argv(step, {"BUNDLE_PATH": str(bundle)}))

    assert result.exit_code == 0, result.output
    mock_gh.assert_called_once_with([
        "attestation",
        "verify",
        str(binary),
        "--repo",
        REPOSITORY,
        "--signer-repo",
        DEFAULT_REPO,
    ])
    assert (project / f"{binary.name}.attestation.json").is_file()


def test_manpages_steps(project):
    """The tarball the attest step names is the one the pack step writes."""
    man = project / "man"
    man.mkdir()
    (man / "papaya.1").write_text(".TH PAPAYA 1\n", encoding="UTF-8")
    pack = find_step(ENGINE, "manpages", "Bundle as tarball")
    attest = next(
        step
        for step in ENGINE["jobs"]["manpages"]["steps"]
        if step.get("id") == "attest-manpages"
    )

    result = invoke(step_argv(pack, {}))

    assert result.exit_code == 0, result.output
    tarball = project / result.stdout.strip()
    assert tarball.is_file()
    # The attest step spells the name from the same metadata key.
    subject = attest["with"]["subject-path"]
    assert subject == (
        "${{ format('{0}.tar.gz', fromJSON(inputs.metadata).manpages_asset_name) }}"
    )
    assert tarball.name == f"{Metadata().manpages_asset_name}.tar.gz"

    bundle = write_bundle(project / "attestation.json", tarball.name)
    verify = find_step(ENGINE, "manpages", "Verify tarball attestation")
    upload = find_step(ENGINE, "manpages", "Attach to release draft")
    env = {"BUNDLE_PATH": str(bundle), "CURRENT_VERSION": VERSION}
    with (
        patch("repomatic.release.attestation.run_gh_command") as mock_verify,
        patch("repomatic.github.releases.run_gh_command") as mock_upload,
    ):
        assert invoke(step_argv(verify, env)).exit_code == 0
        assert invoke(step_argv(upload, env)).exit_code == 0

    assert mock_verify.call_args.args[0][:3] == ["attestation", "verify", str(tarball)]
    assert mock_upload.call_args.args[0] == [
        "release",
        "upload",
        f"v{VERSION}",
        str(tarball),
        str(project / f"{tarball.name}.attestation.json"),
        "--repo",
        REPOSITORY,
        "--clobber",
    ]


@pytest.mark.parametrize("landed", (("papaya.zip", "mango.zip"), ("papaya.zip",)))
def test_extra_assets_steps(project, landed):
    """Every declared asset lands on the draft, or the job fails before upload."""
    assets = project / "release-assets"
    assets.mkdir()
    for name in landed:
        (assets / name).write_bytes(name.encode())
    bundle = write_bundle(project / "attestation.json", *landed)
    check = find_step(
        ENGINE,
        "extra-assets",
        "Check every declared asset landed, and verify the attestation",
    )
    upload = find_step(ENGINE, "extra-assets", "Attach assets to release draft")
    env = {
        "BUNDLE_PATH": str(bundle),
        "CURRENT_VERSION": VERSION,
        "PACKAGE_NAME": "papaya",
    }

    with (
        patch("repomatic.release.attestation.run_gh_command") as mock_verify,
        patch("repomatic.github.releases.run_gh_command") as mock_upload,
    ):
        checked = invoke(step_argv(check, env))
        if len(landed) < 2:
            assert checked.exit_code == 1
            assert (
                "::error::Declared release asset mango.zip has no "
                "release-asset-* artifact."
            ) in checked.output
            mock_verify.assert_not_called()
            return
        assert checked.exit_code == 0, checked.output
        assert invoke(step_argv(upload, env)).exit_code == 0

    assert mock_verify.call_count == 2
    assert mock_upload.call_args.args[0][3:6] == [
        str(assets / "mango.zip"),
        str(assets / "papaya-extra-assets.attestation.json"),
        str(assets / "papaya.zip"),
    ]


def test_publish_release_steps(project):
    """The binaries and their aliases reach the draft, which then publishes."""
    compiled = project / "compile-assets"
    compiled.mkdir()
    binary = compiled / f"papaya-{VERSION}-linux-x64.bin"
    binary.write_bytes(b"\x7fELF")
    (compiled / f"papaya-{VERSION}-py3-none-any.whl").touch()
    upload = find_step(
        ENGINE, "publish-release", "Upload binaries and attestation bundles to release"
    )
    publish = find_step(ENGINE, "publish-release", "Publish release")
    env = {"CURRENT_VERSION": VERSION, "REPO": REPOSITORY}

    with (
        patch("repomatic.github.releases.run_gh_command") as mock_upload,
        patch("repomatic.cli.tools.run_tool", return_value=0) as mock_run,
    ):
        assert invoke(step_argv(upload, env)).exit_code == 0
        assert invoke(step_argv(publish, env)).exit_code == 0

    # The versioned binary and its versionless alias, the package left out.
    assert mock_upload.call_args.args[0] == [
        "release",
        "upload",
        f"v{VERSION}",
        str(binary),
        str(compiled / "papaya-linux-x64.bin"),
        "--repo",
        REPOSITORY,
        "--clobber",
    ]
    assert mock_run.call_args.args[0] == "gh"
    assert mock_run.call_args.kwargs["extra_args"] == (
        "release",
        "edit",
        f"v{VERSION}",
        "--draft=false",
        "--repo",
        REPOSITORY,
    )


# ---------------------------------------------------------------------------
# .github/actions/publish-pypi/action.yaml
# ---------------------------------------------------------------------------


def test_wait_pypi_step(tmp_path):
    """The composite action runs the CLI of the tree it was fetched with."""
    action_dir = REPO_ROOT / ".github" / "actions" / "publish-pypi"
    action = yaml.safe_load((action_dir / "action.yaml").read_text(encoding="UTF-8"))
    step = next(
        step
        for step in action["runs"]["steps"]
        if step.get("name") == "Wait for PyPI propagation"
    )
    # The project the CLI runs from is the repository the action sits in.
    project = re.search(r'--project "\$\{GITHUB_ACTION_PATH\}/([./]+)"', step["run"])
    assert project is not None
    assert (action_dir / project.group(1)).resolve() == REPO_ROOT.resolve()

    (tmp_path / f"papaya-{VERSION}.tar.gz").touch()
    with (
        patch("repomatic.pypi.get_json_soft", return_value={"info": {}}),
        patch("repomatic.pypi.time.sleep"),
    ):
        result = invoke(step_argv(step, {"DOWNLOAD_PATH": str(tmp_path)}))
    assert result.exit_code == 0, result.output
    assert "Visible after 1 attempt(s)" in result.output


# ---------------------------------------------------------------------------
# changelog.yaml
# ---------------------------------------------------------------------------


CHANGELOG_WORKFLOW = load_workflow("changelog.yaml")


@pytest.fixture
def release_checkout(tmp_path, monkeypatch, hermetic_git):
    """A repository whose version was just stripped of its `.dev` suffix."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    run_git("init", "--initial-branch=main", str(checkout), cwd=checkout)
    (checkout / ".github" / "workflows").mkdir(parents=True)
    seed_commit(
        checkout,
        "changelog.md",
        f"# Changelog\n\n## [`{VERSION}` (unreleased)]"
        f"(https://github.com/{REPOSITORY}/compare/v1.2.2...main)\n\n- Juicer.\n",
        "Seed changelog",
    )
    seed_commit(
        checkout,
        "pyproject.toml",
        f'[project]\nname = "papaya"\nversion = "{VERSION}"\n\n'
        f'[tool.bumpversion]\ncurrent_version = "{VERSION}"\n',
        "Seed project",
    )
    monkeypatch.chdir(checkout)
    monkeypatch.setenv("GITHUB_REPOSITORY", REPOSITORY)
    return checkout


def test_prepare_release_commit_steps(release_checkout, tmp_path, monkeypatch):
    """The freeze and the unfreeze commits carry the messages the lane reads."""
    output = tmp_path / "github-output.txt"
    env = {"GITHUB_OUTPUT": str(output)}
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    freeze = find_step(CHANGELOG_WORKFLOW, "prepare-release", "Create freeze commit")
    unfreeze = find_step(
        CHANGELOG_WORKFLOW, "prepare-release", "Create unfreeze commit"
    )

    result = invoke(step_argv(freeze, env))

    assert result.exit_code == 0, result.output
    assert output.read_text(encoding="UTF-8") == f"version={VERSION}\n"
    assert run_git("log", "-1", "--format=%s", cwd=release_checkout).strip() == (
        f"[changelog] Release v{VERSION}"
    )
    changelog = (release_checkout / "changelog.md").read_text(encoding="UTF-8")
    assert "(unreleased)" not in changelog

    # The patch bump the steps in between apply.
    pyproject = release_checkout / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text(encoding="UTF-8").replace(
            f'current_version = "{VERSION}"', 'current_version = "1.2.4.dev0"'
        ),
        encoding="UTF-8",
    )
    result = invoke(step_argv(unfreeze, {}))

    assert result.exit_code == 0, result.output
    assert run_git("log", "-1", "--format=%s", cwd=release_checkout).strip() == (
        f"[changelog] Post-release bump v{VERSION} → v1.2.4.dev0"
    )
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=release_checkout,
        capture_output=True,
        text=True,
        encoding="UTF-8",
        check=True,
    )
    assert status.stdout == ""


@pytest.mark.parametrize("allowed", (True, False))
def test_bump_version_step(project, monkeypatch, allowed):
    """An allowed bump relocks before its pull request, a stale one closes."""
    step = next(
        step
        for step in CHANGELOG_WORKFLOW["jobs"]["bump-version"]["steps"]
        if "bump-version" in (step.get("run") or "")
    )
    calls: list[str] = []
    monkeypatch.setattr(
        "repomatic.cli.release.is_version_bump_allowed", lambda part: allowed
    )

    def run_tool(name: str, args: list[str]) -> int:
        calls.append(f"{name} {' '.join(args)}")
        return 0

    def close(part: str) -> list[int]:
        calls.append(f"close {part}")
        return []

    monkeypatch.setattr("repomatic.cli.release.run_tool", run_tool)
    monkeypatch.setattr("repomatic.cli.release.relock", lambda: calls.append("lock"))
    monkeypatch.setattr(
        "repomatic.cli.release.render_pr",
        lambda *args, **kwargs: PrContent("Title", "Body", "Message"),
    )
    upserted = {}

    def upsert(**kwargs):
        calls.append("pr")
        upserted.update(kwargs)
        return PrSyncResult(PrOperation.CREATED, kwargs["branch"], number=12)

    monkeypatch.setattr("repomatic.cli.release.upsert_pr", upsert)
    monkeypatch.setattr("repomatic.cli.release.close_stale_bump_prs", close)

    result = invoke(step_argv(step, {"PART": "minor"}))

    assert result.exit_code == 0, result.output
    if not allowed:
        assert calls == ["close minor"]
        return
    assert calls == ["bump-my-version bump --verbose minor", "lock", "pr"]
    assert upserted["branch"] == "minor-version-increment"
    assert upserted["draft"] is True
    assert upserted["assignees"] == ("kevin",)
