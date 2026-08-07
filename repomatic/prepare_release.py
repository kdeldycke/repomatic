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

"""Prepare a release by updating changelog, citation, install guide, and workflow files.

A release cycle produces exactly two commits that **must** be merged via
"Rebase and merge" (never squash):

1. **Freeze commit** (`[changelog] Release vX.Y.Z`):

   - Strips the `.dev0` suffix from the version.
   - Finalizes the changelog date and comparison URL.
   - Freezes workflow action references: `@main` → `@vX.Y.Z`.
   - Freezes CLI invocations: `--from . repomatic` → `'repomatic==X.Y.Z'`.
   - Freezes the install guide's binary download URLs to versioned release paths.
   - Pins the install guide's versioned CLI examples to the release.
   - Sets the release date in `citation.cff`.

2. **Unfreeze commit** (`[changelog] Post-release bump vX.Y.Z → vX.Y.(Z+1)`):

   - Reverts action references: `@vX.Y.Z` → `@main`.
   - Reverts CLI invocations back to local source for dogfooding.
   - Bumps the version with a `.dev0` suffix.
   - Adds a new unreleased changelog section.

The auto-tagging job in `release.yaml` depends on these being **separate
commits** — it uses `release_commits_matrix` to identify and tag only the
freeze commit. Squash-merging would collapse both into one, breaking the
tagging logic. See the `detect-squash-merge` job for the safeguard.

Both operations are idempotent: re-running on an already-frozen or
already-unfrozen tree is a no-op.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from functools import cached_property
from pathlib import Path

from .changelog import Changelog
from .config import load_repomatic_config
from .metadata import Metadata

SELF_PIN_COOLDOWN_EXEMPTION = "--exclude-newer-package repomatic=P0D"
"""uv escape hatch letting a just-published repomatic install under the cooldown.

Every workflow exports a `UV_EXCLUDE_NEWER` covering all package resolution (see
`claude.md` § Cooldown on every install), and it applies to the frozen
`'repomatic==X.Y.Z'` self-pin like any other requirement. That pin moves in
lockstep with the `uses:` refs pointing at the same tag, so the version it names
is always minutes old: without an exemption every downstream repo would fail to
resolve it until the window elapsed. A zero-length window sets that one package's
cutoff to "now", leaving the rest of the tree gated.

uv exposes no environment variable for `--exclude-newer-package`, so the
exemption has to ride on the command line, which is why the freeze splices it in
beside the pin instead of the workflows declaring it once.

Spelled as the ISO 8601 `P0D` rather than the `"0 day"` used in
`pyproject.toml`'s `exclude-newer-package` table: the flag travels through YAML
folded scalars into a shell, where the space in `0 day` would need quoting that
survives both. `P0D` needs none.

```{caution}
Every character here lands on 80-odd already-long workflow lines at freeze time.
`tests/test_prepare_release.py` simulates a freeze and fails if the result
breaches yamllint's 120-column cap, so lengthening this string means reflowing
the workflows that no longer fit.
```
"""


class PrepareRelease:
    """Prepare files for a release by updating dates, URLs, and removing warnings."""

    def __init__(
        self,
        changelog_path: Path | None = None,
        citation_path: Path | None = None,
        workflow_dir: Path | None = None,
        install_path: Path | None = None,
        default_branch: str = "main",
    ) -> None:
        self.changelog_path = (
            changelog_path or Path(load_repomatic_config().changelog_location).resolve()
        )
        self.citation_path = citation_path or Path("./citation.cff").resolve()
        self.workflow_dir = workflow_dir or Path("./.github/workflows").resolve()
        self.install_path = install_path or Path("./docs/install.md").resolve()
        self.default_branch = default_branch
        self.modified_files: list[Path] = []

    @cached_property
    def current_version(self) -> str:
        """Extract current version from the bump-my-version config.

        Delegates discovery to {meth}`.Metadata.get_current_version`, which
        searches `.bumpversion.toml` then `pyproject.toml`.
        """
        version = Metadata.get_current_version()
        if version is None:
            raise RuntimeError(
                "No bump-my-version config found "
                "(searched .bumpversion.toml and pyproject.toml).",
            )
        logging.info(f"Current version: {version}")
        return version

    @cached_property
    def package_name(self) -> str | None:
        """Canonical PyPI package name, used to spot pinned install examples.

        Delegates discovery to {attr}`.Metadata.package_name`, which reads
        `pyproject.toml`.
        """
        return Metadata().package_name

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
        """Update the `date-released` field in citation.cff.

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

    def _workflow_files(self) -> list[Path]:
        """Enumerate the workflow files under {attr}`workflow_dir`.

        Covers both YAML extensions: `repomatic init` writes `.yaml`, but
        downstream-authored workflows may use `.yml`, and a file skipped here
        would ship with unfrozen (mutable) refs in the release.
        """
        return sorted(
            path
            for pattern in ("*.yaml", "*.yml")
            for path in self.workflow_dir.glob(pattern)
        )

    @cached_property
    def composite_action_names(self) -> list[str]:
        """Discover composite action directories under `.github/actions/`.

        Enumerates every `.github/actions/*/action.yaml` (or `.yml`) and
        returns the directory names. New composite actions automatically
        participate in freeze/unfreeze without requiring code changes here.

        :return: Sorted list of composite action directory names.
        """
        actions_dir = self.workflow_dir.parent / "actions"
        if not actions_dir.exists():
            return []
        names = {
            path.parent.name
            for pattern in ("*/action.yaml", "*/action.yml")
            for path in actions_dir.glob(pattern)
        }
        return sorted(names)

    def freeze_workflow_urls(self) -> int:
        """Replace workflow URLs from default branch to versioned tag.

        This is part of the **freeze** step: it freezes workflow references to
        the release tag so released versions reference immutable URLs.

        Replaces ``/repomatic/{default_branch}/`` with ``/repomatic/v{version}/``
        and every ``/repomatic/.github/actions/{name}@{default_branch}`` with
        ``/repomatic/.github/actions/{name}@v{version}`` across every workflow
        file under :attr:`workflow_dir`. Composite action names are discovered
        from :attr:`composite_action_names`.

        :return: Number of files modified.
        """
        if not self.workflow_dir.exists():
            logging.debug(f"Workflow directory not found: {self.workflow_dir}")
            return 0

        count = 0
        # URL pattern: /repomatic/main/ -> /repomatic/v1.2.3/.
        url_search = f"/repomatic/{self.default_branch}/"
        url_replace = f"/repomatic/v{self.current_version}/"
        # Action reference pattern: /repomatic/.github/actions/{name}@main -> @v1.2.3.
        action_pairs = [
            (
                f"/repomatic/.github/actions/{name}@{self.default_branch}",
                f"/repomatic/.github/actions/{name}@v{self.current_version}",
            )
            for name in self.composite_action_names
        ]

        for yaml_file in self._workflow_files():
            original = yaml_file.read_text(encoding="UTF-8")
            content = original.replace(url_search, url_replace)
            for action_search, action_replace in action_pairs:
                content = content.replace(action_search, action_replace)
            if self._update_file(yaml_file, content, original):
                count += 1

        return count

    @staticmethod
    def _replace_skip_comments(
        content: str,
        search: str,
        replacement: str,
        comment_prefix: str = "#",
    ) -> str:
        """Replace a string only in non-comment lines.

        Comment lines (where the first non-whitespace character is
        `comment_prefix`) are preserved unchanged. This prevents
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

    def freeze_install_download_urls(self, version: str) -> bool:
        """Replace binary download URLs in the install guide with versioned paths.

        This is part of the **freeze** step: it freezes the install guide's
        download links to a specific GitHub release so users get explicit,
        versioned URLs instead of the `/releases/latest/download/` redirect.
        The versionless redirect cannot resolve because release assets are
        version-stamped (`repomatic-{version}-linux-arm64.bin`).

        Handles two input forms:

        - **Initial** (never frozen):
          `/releases/latest/download/repomatic-linux-arm64.bin`
        - **Previously frozen**:
          `/releases/download/v6.0.0/repomatic-6.0.0-linux-arm64.bin`

        Both are transformed to:
        ``/releases/download/v{version}/repomatic-{version}-linux-arm64.bin``

        ```{note}
        No unfreeze method is needed. Unlike workflow URLs (which toggle
        `@main` ↔ `@vX.Y.Z`), download URLs ratchet forward — they always
        point to a specific release. After unfreeze, the install guide still
        shows the last release's URLs, which is correct for users wanting
        stable binaries.
        ```

        :param version: The release version to freeze to.
        :return: True if the file was modified.
        """
        if not self.install_path.exists():
            logging.debug(f"Install guide not found: {self.install_path}")
            return False

        original = self.install_path.read_text(encoding="UTF-8")

        # Pass 1: Rewrite URL paths from /releases/latest/download/ or
        # /releases/download/vX.Y.Z/ to /releases/download/v{version}/.
        content = re.sub(
            r"/releases/(?:latest/download|download/v[\d.]+)/",
            f"/releases/download/v{version}/",
            original,
        )

        # Pass 2: Rewrite binary filenames (in both URL and display text)
        # from repomatic-target.ext or repomatic-X.Y.Z-target.ext to
        # repomatic-{version}-target.ext.
        content = re.sub(
            r"repomatic(?:-[\d.]+)?-"
            r"((?:linux|macos|windows)-(?:arm64|x64))\.(bin|exe)",
            f"repomatic-{version}-\\1.\\2",
            content,
        )

        return self._update_file(self.install_path, content, original)

    def freeze_install_cli_version(self, version: str) -> bool:
        """Pin the install guide's versioned CLI examples to the release.

        This is part of the **freeze** step: the install guide's `Specific
        version` tab demonstrates a pinned invocation (`uvx {package}@X.Y.Z`
        or a `{package}==X.Y.Z` requirement), which must always showcase the
        latest release. Without this pass the pinned example silently rots
        (click-extra's install guide sat on a 14-releases-old pin).

        ```{note}
        Like {meth}`freeze_install_download_urls`, this ratchets forward with
        no unfreeze: after a release the examples keep demonstrating that
        release, which is what readers should copy until the next one ships.
        ```

        :param version: The release version to pin the examples to.
        :return: True if the file was modified.
        """
        if not self.install_path.exists():
            logging.debug(f"Install guide not found: {self.install_path}")
            return False
        if not self.package_name:
            logging.warning(
                "No package name found in pyproject.toml: "
                "skipping install guide CLI version pinning.",
            )
            return False

        original = self.install_path.read_text(encoding="UTF-8")

        # Match `{package}@X.Y.Z` (uvx pin) and `{package}==X.Y.Z` (PEP 508
        # pin), leaving `@main`, git refs, and other packages' pins untouched.
        # The leading boundary keeps distro-prefixed names (python-{package})
        # out of scope; the optional `.devN` tail absorbs development pins.
        pattern = re.compile(
            rf"(?<![\w-])({re.escape(self.package_name)}(?:@|==))"
            rf"\d+(?:\.\d+)*(?:\.dev\d+)?",
        )
        content = pattern.sub(rf"\g<1>{version}", original)

        return self._update_file(self.install_path, content, original)

    def freeze_cli_version(self, version: str) -> int:
        """Replace local source CLI invocations with a frozen PyPI version.

        This is part of the **freeze** step: it freezes `repomatic`
        invocations to a specific PyPI version so the released workflow files
        reference a published package. Downstream repos that check out a tagged
        release will install from PyPI rather than expecting a local source
        tree.

        Replaces `--from . repomatic` with ``'repomatic=={version}'`` in all
        workflow YAML files. Comment lines (starting with `#`) are skipped to
        avoid corrupting explanatory comments.

        The pin is spliced in behind {data}`SELF_PIN_COOLDOWN_EXEMPTION`, which
        is what keeps a release installable the minute it is published despite
        the workflow-wide cooldown. Local source needs no exemption, so `main`
        carries none between releases.

        :param version: The PyPI version to freeze to.
        :return: Number of files modified.
        """
        if not self.workflow_dir.exists():
            logging.debug(f"Workflow directory not found: {self.workflow_dir}")
            return 0

        count = 0
        search = "--from . repomatic"
        yaml_replace = f"{SELF_PIN_COOLDOWN_EXEMPTION} 'repomatic=={version}'"

        for workflow_file in self._workflow_files():
            original = workflow_file.read_text(encoding="UTF-8")
            content = self._replace_skip_comments(original, search, yaml_replace)
            if self._update_file(workflow_file, content, original):
                count += 1

        return count

    def unfreeze_cli_version(self) -> int:
        """Replace frozen PyPI CLI invocations with local source.

        This is part of the **unfreeze** step: it reverts `repomatic`
        invocations back to local source (`--from . repomatic`) for the next
        development cycle on `main`.

        Replaces `'repomatic==X.Y.Z'` (quoted, in YAML) with
        `--from . repomatic`, taking {data}`SELF_PIN_COOLDOWN_EXEMPTION` with it
        when the freeze put one there: local source resolves from the working
        tree, so it never needs the escape hatch. The prefix is optional in the
        pattern so a workflow frozen by an older release still unfreezes cleanly.
        Comment lines are skipped (see {meth}`freeze_cli_version`).

        :return: Number of files modified.
        """
        if not self.workflow_dir.exists():
            logging.debug(f"Workflow directory not found: {self.workflow_dir}")
            return 0

        count = 0
        yaml_pattern = re.compile(
            rf"(?:{re.escape(SELF_PIN_COOLDOWN_EXEMPTION)} )?'repomatic==[\d.]+'"
        )
        replace = "--from . repomatic"

        for workflow_file in self._workflow_files():
            original = workflow_file.read_text(encoding="UTF-8")
            content = self._sub_skip_comments(original, yaml_pattern, replace)
            if self._update_file(workflow_file, content, original):
                count += 1

        return count

    def unfreeze_workflow_urls(self) -> int:
        """Replace workflow URLs from versioned tag back to default branch.

        This is part of the **unfreeze** step: it reverts workflow references back
        to the default branch for the next development cycle.

        Replaces ``/repomatic/v{version}/`` with ``/repomatic/{default_branch}/``
        and every ``/repomatic/.github/actions/{name}@v{version}`` with
        ``/repomatic/.github/actions/{name}@{default_branch}`` across every
        workflow file under :attr:`workflow_dir` (the same set as
        :meth:`freeze_workflow_urls`). Composite action names are discovered
        from :attr:`composite_action_names`.

        :return: Number of files modified.
        """
        if not self.workflow_dir.exists():
            logging.debug(f"Workflow directory not found: {self.workflow_dir}")
            return 0

        count = 0
        # URL pattern: /repomatic/v1.2.3/ -> /repomatic/main/.
        url_search = f"/repomatic/v{self.current_version}/"
        url_replace = f"/repomatic/{self.default_branch}/"
        # Action reference pattern: @v1.2.3 -> @main.
        action_pairs = [
            (
                f"/repomatic/.github/actions/{name}@v{self.current_version}",
                f"/repomatic/.github/actions/{name}@{self.default_branch}",
            )
            for name in self.composite_action_names
        ]

        for yaml_file in self._workflow_files():
            original = yaml_file.read_text(encoding="UTF-8")
            content = original.replace(url_search, url_replace)
            for action_search, action_replace in action_pairs:
                content = content.replace(action_search, action_replace)
            if self._update_file(yaml_file, content, original):
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

        # Unconditional: every downstream repo following the install-page
        # recipe carries a pinned CLI example, not just repos that dogfood
        # repomatic's own workflows.
        self.freeze_install_cli_version(self.current_version)

        if update_workflows:
            self.freeze_workflow_urls()
            self.freeze_cli_version(self.current_version)
            self.freeze_install_download_urls(self.current_version)

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
