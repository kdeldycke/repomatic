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

"""Prepare a release by updating changelog, citation, and workflow files.

A release cycle produces exactly two commits that **must** be merged via
"Rebase and merge" (never squash):

1. **Freeze commit** (``[changelog] Release vX.Y.Z``):

   - Strips the ``.dev0`` suffix from the version.
   - Finalizes the changelog date and comparison URL.
   - Freezes workflow action references: ``@main`` → ``@vX.Y.Z``.
   - Freezes CLI invocations: ``--from . repomatic`` → ``'repomatic==X.Y.Z'``.
   - Sets the release date in ``citation.cff``.

2. **Unfreeze commit** (``[changelog] Post-release bump vX.Y.Z → vX.Y.(Z+1)``):

   - Reverts action references: ``@vX.Y.Z`` → ``@main``.
   - Reverts CLI invocations back to local source for dogfooding.
   - Bumps the version with a ``.dev0`` suffix.
   - Adds a new unreleased changelog section.

The auto-tagging job in ``release.yaml`` depends on these being **separate
commits** — it uses ``release_commits_matrix`` to identify and tag only the
freeze commit. Squash-merging would collapse both into one, breaking the
tagging logic. See the ``detect-squash-merge`` job for the safeguard.

Both operations are idempotent: re-running on an already-frozen or
already-unfrozen tree is a no-op.
"""

from __future__ import annotations

import logging
import re
import sys
from datetime import datetime, timezone
from functools import cached_property
from pathlib import Path

from .changelog import Changelog

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[import-not-found]


class ReleasePrep:
    """Prepare files for a release by updating dates, URLs, and removing warnings."""

    def __init__(
        self,
        changelog_path: Path | None = None,
        citation_path: Path | None = None,
        workflow_dir: Path | None = None,
        default_branch: str = "main",
    ) -> None:
        self.changelog_path = changelog_path or Path("./changelog.md").resolve()
        self.citation_path = citation_path or Path("./citation.cff").resolve()
        self.workflow_dir = workflow_dir or Path("./.github/workflows").resolve()
        self.default_branch = default_branch
        self.modified_files: list[Path] = []

    @cached_property
    def current_version(self) -> str:
        """Extract current version from bump-my-version config in pyproject.toml."""
        config_file = Path("./pyproject.toml").resolve()
        logging.info(f"Reading version from {config_file}")
        config = tomllib.loads(config_file.read_text(encoding="UTF-8"))
        version: str = config["tool"]["bumpversion"]["current_version"]
        logging.info(f"Current version: {version}")
        return version

    @cached_property
    def release_date(self) -> str:
        """Return today's date in UTC as YYYY-MM-DD."""
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _update_file(self, path: Path, content: str, original: str) -> bool:
        """Write content to file if it changed. Return True if modified."""
        if content != original:
            path.write_text(content, encoding="UTF-8")
            self.modified_files.append(path)
            logging.info(f"Updated {path}")
            return True
        logging.debug(f"No changes to {path}")
        return False

    def set_citation_release_date(self) -> bool:
        """Update the ``date-released`` field in citation.cff.

        :return: True if the file was modified.
        """
        if not self.citation_path.exists():
            logging.debug(f"Citation file not found: {self.citation_path}")
            return False

        original = self.citation_path.read_text(encoding="UTF-8")
        content = re.sub(
            r"date-released: \d{4}-\d{2}-\d{2}",
            f"date-released: {self.release_date}",
            original,
            count=1,
        )

        return self._update_file(self.citation_path, content, original)

    def freeze_workflow_urls(self) -> int:
        """Replace workflow URLs from default branch to versioned tag.

        This is part of the **freeze** step: it freezes workflow references to
        the release tag so released versions reference immutable URLs.

        Replaces ``/repomatic/{default_branch}/`` with ``/repomatic/v{version}/``
        and ``/repomatic/.github/actions/...@{default_branch}`` with
        ``/repomatic/.github/actions/...@v{version}`` in all workflow YAML files.

        :return: Number of files modified.
        """
        if not self.workflow_dir.exists():
            logging.debug(f"Workflow directory not found: {self.workflow_dir}")
            return 0

        count = 0
        # URL pattern: /repomatic/main/ -> /repomatic/v1.2.3/
        url_search = f"/repomatic/{self.default_branch}/"
        url_replace = f"/repomatic/v{self.current_version}/"
        # Action reference pattern: /repomatic/.github/...@main -> @v1.2.3
        action_search = f"/repomatic/.github/actions/pr-metadata@{self.default_branch}"
        action_replace = (
            f"/repomatic/.github/actions/pr-metadata@v{self.current_version}"
        )

        for workflow_file in self.workflow_dir.glob("*.yaml"):
            original = workflow_file.read_text(encoding="UTF-8")
            content = original.replace(url_search, url_replace)
            content = content.replace(action_search, action_replace)
            if self._update_file(workflow_file, content, original):
                count += 1

        return count

    @cached_property
    def _json5_files(self) -> list[Path]:
        """Return renovate.json5 files that need CLI freeze/unfreeze.

        Includes the repo-root ``renovate.json5`` and the bundled copy under
        ``repomatic/data/``.
        """
        # Repo root is two levels up from .github/workflows/.
        repo_root = self.workflow_dir.parent.parent
        return [
            repo_root / "renovate.json5",
            repo_root / "repomatic" / "data" / "renovate.json5",
        ]

    @staticmethod
    def _replace_skip_comments(
        content: str,
        search: str,
        replacement: str,
        comment_prefix: str = "#",
    ) -> str:
        """Replace a string only in non-comment lines.

        Comment lines (where the first non-whitespace character is
        ``comment_prefix``) are preserved unchanged. This prevents
        freeze/unfreeze operations from corrupting explanatory comments that
        mention the search string.

        :param content: The file content to process.
        :param search: The literal string to find and replace.
        :param replacement: The string to substitute.
        :param comment_prefix: The character that marks a comment line.
        :return: The content with replacements applied to non-comment lines.
        """
        lines = content.splitlines(keepends=True)
        return "".join(
            line
            if line.lstrip().startswith(comment_prefix)
            else line.replace(search, replacement)
            for line in lines
        )

    @staticmethod
    def _sub_skip_comments(
        content: str,
        pattern: re.Pattern[str],
        replacement: str,
        comment_prefix: str = "#",
    ) -> str:
        """Regex-substitute only in non-comment lines.

        :param content: The file content to process.
        :param pattern: The compiled regex pattern to match.
        :param replacement: The string to substitute.
        :param comment_prefix: The character that marks a comment line.
        :return: The content with substitutions applied to non-comment lines.
        """
        lines = content.splitlines(keepends=True)
        return "".join(
            line
            if line.lstrip().startswith(comment_prefix)
            else pattern.sub(replacement, line)
            for line in lines
        )

    def freeze_cli_version(self, version: str) -> int:
        """Replace local source CLI invocations with a frozen PyPI version.

        This is part of the **freeze** step: it freezes ``repomatic``
        invocations to a specific PyPI version so the released workflow files
        reference a published package. Downstream repos that check out a tagged
        release will install from PyPI rather than expecting a local source
        tree.

        Replaces ``--from . repomatic`` with ``'repomatic=={version}'`` in all
        workflow YAML files, and with ``repomatic=={version}`` (unquoted) in
        ``renovate.json5`` files (where the command lives inside ``bash -c '...'``
        and single quotes would break the outer quoting).

        Comment lines in YAML (starting with ``#``) and JSON5 (starting with
        ``//``) are skipped to avoid corrupting explanatory comments.

        :param version: The PyPI version to freeze to.
        :return: Number of files modified.
        """
        if not self.workflow_dir.exists():
            logging.debug(f"Workflow directory not found: {self.workflow_dir}")
            return 0

        count = 0
        search = "--from . repomatic"
        yaml_replace = f"'repomatic=={version}'"

        for workflow_file in self.workflow_dir.glob("*.yaml"):
            original = workflow_file.read_text(encoding="UTF-8")
            content = self._replace_skip_comments(original, search, yaml_replace)
            if self._update_file(workflow_file, content, original):
                count += 1

        # Freeze renovate.json5 files with unquoted form.
        json5_replace = f"repomatic=={version}"
        for json5_file in self._json5_files:
            if not json5_file.exists():
                logging.debug(f"JSON5 file not found: {json5_file}")
                continue
            original = json5_file.read_text(encoding="UTF-8")
            content = self._replace_skip_comments(
                original,
                search,
                json5_replace,
                comment_prefix="//",
            )
            if self._update_file(json5_file, content, original):
                count += 1

        return count

    def unfreeze_cli_version(self) -> int:
        """Replace frozen PyPI CLI invocations with local source.

        This is part of the **unfreeze** step: it reverts ``repomatic``
        invocations back to local source (``--from . repomatic``) for the next
        development cycle on ``main``.

        Replaces ``'repomatic==X.Y.Z'`` (quoted, in YAML) and
        ``repomatic==X.Y.Z`` (unquoted, in ``renovate.json5``) with
        ``--from . repomatic``.

        Comment lines are skipped (see :meth:`freeze_cli_version`).

        :return: Number of files modified.
        """
        if not self.workflow_dir.exists():
            logging.debug(f"Workflow directory not found: {self.workflow_dir}")
            return 0

        count = 0
        yaml_pattern = re.compile(r"'repomatic==[\d.]+'")
        replace = "--from . repomatic"

        for workflow_file in self.workflow_dir.glob("*.yaml"):
            original = workflow_file.read_text(encoding="UTF-8")
            content = self._sub_skip_comments(original, yaml_pattern, replace)
            if self._update_file(workflow_file, content, original):
                count += 1

        # Unfreeze renovate.json5 files (unquoted form).
        json5_pattern = re.compile(r"repomatic==[\d.]+")
        for json5_file in self._json5_files:
            if not json5_file.exists():
                logging.debug(f"JSON5 file not found: {json5_file}")
                continue
            original = json5_file.read_text(encoding="UTF-8")
            content = self._sub_skip_comments(
                original,
                json5_pattern,
                replace,
                comment_prefix="//",
            )
            if self._update_file(json5_file, content, original):
                count += 1

        return count

    def unfreeze_workflow_urls(self) -> int:
        """Replace workflow URLs from versioned tag back to default branch.

        This is part of the **unfreeze** step: it reverts workflow references back
        to the default branch for the next development cycle.

        Replaces ``/repomatic/v{version}/`` with ``/repomatic/{default_branch}/``
        and ``/repomatic/.github/actions/...@v{version}`` with
        ``/repomatic/.github/actions/...@{default_branch}`` in all workflow YAML files.

        :return: Number of files modified.
        """
        if not self.workflow_dir.exists():
            logging.debug(f"Workflow directory not found: {self.workflow_dir}")
            return 0

        count = 0
        # URL pattern: /repomatic/v1.2.3/ -> /repomatic/main/
        url_search = f"/repomatic/v{self.current_version}/"
        url_replace = f"/repomatic/{self.default_branch}/"
        # Action reference pattern: @v1.2.3 -> @main
        action_search = (
            f"/repomatic/.github/actions/pr-metadata@v{self.current_version}"
        )
        action_replace = f"/repomatic/.github/actions/pr-metadata@{self.default_branch}"

        for workflow_file in self.workflow_dir.glob("*.yaml"):
            original = workflow_file.read_text(encoding="UTF-8")
            content = original.replace(url_search, url_replace)
            content = content.replace(action_search, action_replace)
            if self._update_file(workflow_file, content, original):
                count += 1

        return count

    def prepare_release(self, update_workflows: bool = False) -> list[Path]:
        """Run all freeze steps to prepare the release commit.

        :param update_workflows: If True, also freeze workflow URLs to versioned tag
            and freeze CLI invocations to the current version.
        :return: List of modified files.
        """
        self.modified_files = []

        if Changelog.freeze_file(
            self.changelog_path,
            version=self.current_version,
            release_date=self.release_date,
            default_branch=self.default_branch,
        ):
            self.modified_files.append(self.changelog_path)

        self.set_citation_release_date()

        if update_workflows:
            self.freeze_workflow_urls()
            self.freeze_cli_version(self.current_version)

        return self.modified_files

    def post_release(self, update_workflows: bool = False) -> list[Path]:
        """Run all unfreeze steps to prepare the post-release commit.

        :param update_workflows: If True, unfreeze workflow URLs back to default
            branch and unfreeze CLI invocations back to local source.
        :return: List of modified files.
        """
        self.modified_files = []

        if update_workflows:
            self.unfreeze_workflow_urls()
            self.unfreeze_cli_version()

        return self.modified_files
