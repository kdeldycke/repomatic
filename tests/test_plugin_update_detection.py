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

"""Which manifest's `version` the Claude Code CLI reads to detect an update.

Two manifests carry a version: the marketplace entry, and the plugin's own
`.claude-plugin/plugin.json`. The release freeze stamps both, so the plugin ships
correctly whichever one a client reads. The docs state which one the CLI reads,
and this is what keeps that statement honest.

Measured against Claude Code 2.1.274: the CLI compares `plugin.json` and ignores
the catalog entry, for a `path` source and a `git-subdir` source alike. The
Desktop app, which the docs also cover, is out of reach of any test here.

```{caution}
This measures a behavior Anthropic can change without a version bump, so a
failure here is a signal to re-read the docs page, not a bug in this repository.
```

The scene is built under `tmp_path` with its own `CLAUDE_CONFIG_DIR`, and the
plugin's remote is a `file://` URL onto a second scratch repository, so parallel
workers cannot collide. Nothing leaves the machine either: the `egress` fixture
turns off the CLI's own traffic and refuses whatever is left.
"""

from __future__ import annotations

import json
import os
import shutil
import socketserver
import subprocess
import threading

import pytest

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

pytestmark = [
    # Nothing in the repomatic package is exercised here, so the matrix run that
    # holds the coverage floor loses nothing by filtering this out.
    pytest.mark.once,
]

# Kept out of `pytestmark`: the test of the `egress` fixture runs no CLI.
needs_cli = pytest.mark.skipif(
    shutil.which("claude") is None,
    reason="needs the Claude Code CLI to answer for its own behavior",
)

PLUGIN_NAME = "citylog"
MARKETPLACE_NAME = "orchard"
BASE_VERSION = "1.0.0"
BUMPED_VERSION = "9.9.9"

TIMEOUT = 120
"""Seconds any one CLI call may take before the probe fails instead of hanging."""

OFFLINE_SWITCH = "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"
"""The CLI's own setting for telemetry, error reports and update checks.

Measured against Claude Code 2.1.284: each plugin command that changes
something asks for `api.anthropic.com:443` once, and asks for nothing with this
variable set.
"""

PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
"""The variables the CLI reads to find its way out, in upper case."""


@pytest.fixture
def egress(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Keep the CLI on this machine, and collect what it still asks to reach.

    The suite runs offline, and `tests/conftest.py` refuses every unmocked
    call the Python side makes. The CLI is a child process with a client of its
    own, which none of those stubs reach, so it gets two answers of its own.
    {data}`OFFLINE_SWITCH` stops the calls at their source. A proxy that
    forwards nothing takes whatever the switch does not cover, and keeps each
    request line for the test to assert on.

    A sandbox can refuse the bind on the loopback. The test is then skipped:
    without the proxy, nothing proves that the CLI stays on this machine.

    :return: The request line of every call the proxy refused.
    """
    asked: list[str] = []

    class Refusal(socketserver.StreamRequestHandler):
        # A client that connects and stays silent must not hold the proxy.
        timeout = 5

        def handle(self) -> None:
            asked.append(self.rfile.readline().decode("ISO-8859-1").strip())
            self.wfile.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")

    try:
        proxy = socketserver.TCPServer(("127.0.0.1", 0), Refusal)
    except PermissionError as error:
        pytest.skip(f"cannot bind the proxy on the loopback: {error}")
    with proxy:
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        address = f"http://127.0.0.1:{proxy.server_address[1]}"
        monkeypatch.setenv(OFFLINE_SWITCH, "1")
        for name in PROXY_VARIABLES:
            monkeypatch.setenv(name, address)
            monkeypatch.setenv(name.lower(), address)
        # Only the loopback skips the proxy: nothing there leaves the machine.
        for name in ("NO_PROXY", "no_proxy"):
            monkeypatch.setenv(name, "localhost,127.0.0.1,::1")
        yield asked
        proxy.shutdown()


def _claude(config: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the CLI against a throwaway configuration directory."""
    return subprocess.run(
        ("claude", *args),
        capture_output=True,
        # A non-zero exit is data here: an uninstall of a plugin that is not
        # installed is part of the normal flow between cases.
        check=False,
        encoding="UTF-8",
        env=dict(os.environ, CLAUDE_CONFIG_DIR=str(config)),
        text=True,
        timeout=TIMEOUT,
    )


def _git(cwd: Path, *args: str) -> None:
    """Run git without signing, which would block on an approval prompt."""
    subprocess.run(
        ("git", "-c", "commit.gpgsign=false", *args),
        capture_output=True,
        check=True,
        cwd=cwd,
        encoding="UTF-8",
        text=True,
        timeout=TIMEOUT,
    )


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="UTF-8")


def _catalog(origin: Path, version: str) -> dict:
    return {
        "name": MARKETPLACE_NAME,
        "owner": {"name": "Orchard"},
        "plugins": [
            {
                "name": PLUGIN_NAME,
                "description": "Records the weather in a few cities.",
                "source": {
                    "source": "git-subdir",
                    "url": f"file://{origin}",
                    "path": "plug",
                    "ref": "main",
                },
                "version": version,
            },
        ],
    }


def _manifest(version: str) -> dict:
    return {
        "name": PLUGIN_NAME,
        "description": "Records the weather in a few cities.",
        "version": version,
    }


def _update_outcome(config: Path) -> str | None:
    """Ask the CLI to update, and report what it decided."""
    result = _claude(config, "plugin", "update", PLUGIN_NAME, "--json", "-y")
    for line in reversed(result.stdout.strip().splitlines()):
        try:
            report: dict[str, str] = json.loads(line)
        except json.JSONDecodeError:
            continue
        return report.get("updateOutcome")
    return None


@needs_cli
def test_cli_update_detection_reads_the_plugin_manifest(
    tmp_path: Path, egress: list[str]
) -> None:
    """Bump each version on its own and see which one the CLI reacts to.

    The four cases are one test rather than four parametrized ones because two of
    them are controls for the other two: a run where the controls misbehave says
    nothing about the question, and splitting them would let that run report a
    verdict anyway.
    """
    origin = tmp_path / "origin"
    catalog = tmp_path / "catalog"
    config = tmp_path / "config"

    # The remote holding the plugin directory the catalog clones.
    _write_json(
        origin / "plug" / ".claude-plugin" / "plugin.json", _manifest(BASE_VERSION)
    )
    skill = origin / "plug" / "skills" / "log-weather"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: log-weather\n"
        "description: Write today's weather into a log.\n---\n\n"
        "Ask for a city, then append one line to the log.\n",
        encoding="UTF-8",
    )
    _git(origin, "init", "--quiet", "--initial-branch=main")
    _git(origin, "add", "-A")
    _git(origin, "commit", "--quiet", "-m", "Seed")

    _write_json(
        catalog / ".claude-plugin" / "marketplace.json", _catalog(origin, BASE_VERSION)
    )

    added = _claude(config, "plugin", "marketplace", "add", str(catalog))
    assert added.returncode == 0, (
        f"could not add the scratch marketplace: {added.stderr}"
    )

    def set_versions(catalog_version: str, plugin_version: str) -> None:
        _write_json(
            catalog / ".claude-plugin" / "marketplace.json",
            _catalog(origin, catalog_version),
        )
        _write_json(
            origin / "plug" / ".claude-plugin" / "plugin.json",
            _manifest(plugin_version),
        )
        _git(origin, "add", "-A")
        _git(origin, "commit", "--allow-empty", "--quiet", "-m", "Bump")
        _claude(config, "plugin", "marketplace", "update", MARKETPLACE_NAME)

    def outcome_after(catalog_version: str, plugin_version: str) -> str | None:
        """Reinstall at the base version, bump, and report the CLI's decision."""
        _claude(config, "plugin", "uninstall", PLUGIN_NAME, "-y")
        set_versions(BASE_VERSION, BASE_VERSION)
        installed = _claude(
            config, "plugin", "install", f"{PLUGIN_NAME}@{MARKETPLACE_NAME}", "-y"
        )
        assert installed.returncode == 0, f"could not install: {installed.stderr}"
        set_versions(catalog_version, plugin_version)
        return _update_outcome(config)

    # Controls first: a run that fails either one is void, and says so rather
    # than reporting a verdict on the question below.
    assert outcome_after(BASE_VERSION, BASE_VERSION) == "up_to_date", (
        "control failed: the CLI offered an update with neither version bumped, "
        "so this probe cannot tell a detected update from a spurious one"
    )
    assert outcome_after(BUMPED_VERSION, BUMPED_VERSION) == "updated", (
        "control failed: the CLI saw no update with both versions bumped, "
        "so this probe cannot detect an update at all"
    )

    assert outcome_after(BASE_VERSION, BUMPED_VERSION) == "updated", (
        "the CLI no longer reacts to the plugin manifest's version; "
        "re-read docs/claude-code-plugin.md § How the pin moves"
    )
    assert outcome_after(BUMPED_VERSION, BASE_VERSION) == "up_to_date", (
        "the CLI now reacts to the catalog entry's version; "
        "re-read docs/claude-code-plugin.md § How the pin moves"
    )

    assert not egress, (
        f"the CLI asked a proxy for {sorted(set(egress))}, so {OFFLINE_SWITCH} no "
        "longer keeps this scene on the machine; measure what it covers again"
    )


def test_egress_skips_when_the_loopback_refuses_a_bind(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bind that the sandbox refuses skips the test, with the error as reason.

    Any other error of the bind still fails: only `PermissionError` says that
    the machine forbids the proxy.
    """

    def refuse(server: socketserver.TCPServer) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(socketserver.TCPServer, "server_bind", refuse)
    with pytest.raises(pytest.skip.Exception, match="Operation not permitted"):
        request.getfixturevalue("egress")
