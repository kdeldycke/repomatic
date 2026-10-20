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

"""Assemble the body of an automated pull request or issue.

{func}`render_pr` renders a pull request's title, body and commit message from
a {class}`~repomatic.templating.PrTemplate`, filling each argument the template
declares from the CLI, the environment or the GitHub API. The body ends on a
collapsible `<details>` block of workflow metadata, read from an injected
{class}`~repomatic.metadata.core.Metadata` instance (the injection keeps this
module import-cycle-free: `Metadata` pulls in half the package).

{func}`build_pr_body` and {func}`fit_github_body` keep a body within GitHub's
size limit, and {func}`temp_body_file` hands it to `gh` as a file.
"""

from __future__ import annotations

import logging
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .. import __version__
from ..deps.sources import build_release_readiness
from ..templating import PrTemplate, render_template
from ..versions import strip_dev_suffix
from .event import extract_workflow_filename
from .outputs import read_file_output, trim_to_budget
from .releases import release_review_steps

# isort: split
# 8.0.0 shim: re-export for callers not migrated yet.
from ..markdown import (  # noqa: F401
    demote_markdown_headings,
    sanitize_markdown_mentions,
)

# 8.0.0 shim: re-export for callers not migrated yet.
from ..templating import (  # noqa: F401
    footer_opted_out,
    get_template_names,
    template_stem,
)

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Mapping

    from ..config import Config
    from ..metadata.core import Metadata

GITHUB_BODY_MAX_CHARS = 65536
"""GitHub's maximum PR and issue body size, in UTF-16 code units.

GitHub's API rejects longer bodies outright, so an oversized body has to be
trimmed before it is posted. The obvious trim is the wrong one: cutting the
**end** (what a plain `body[:65536]` does) drops whatever sits last, which here
is the refresh tip, the metadata block and the attribution footer: the
navigational parts a reader needs most when a report is too long to read.
{func}`build_pr_body` (PRs) and {func}`fit_github_body` (issues) therefore trim
the leading content instead, so the tail always survives.
"""

_TRUNCATION_NOTICE = "> [!CAUTION]\n> Report truncated to fit GitHub's body size limit."
"""Admonition replacing the content dropped by {func}`build_pr_body` and
{func}`fit_github_body`."""


class TemplateArgumentError(ValueError):
    """A template argument that nothing supplied a value for."""


@dataclass(frozen=True)
class PrContent:
    """A rendered pull request, ready for `gh` and for the commit."""

    title: str
    """The pull request title, empty when no template supplies one."""

    body: str
    """The markdown body, metadata block and attribution footer included."""

    commit_message: str
    """The commit message, empty when no template supplies one."""


def generate_pr_metadata_block(
    md: Metadata, docs_url: str = "", docs_name: str = ""
) -> str:
    """Generate a collapsible metadata block from CI context.

    Reads the `GITHUB_*` environment context from *md* and returns a
    markdown `<details>` block listing the workflow metadata fields.

    :param md: The {class}`~repomatic.metadata.core.Metadata` instance to read
        CI context from.
    :param docs_url: Optional deep link to the job's section of the hosted
        workflows reference, rendered as the leading `Documentation` entry.
        Comes from the template's `docs:` frontmatter field (see
        {attr}`~repomatic.templating.PrTemplate.docs_url`).
    :param docs_name: Label for the documentation link, usually the template
        (operation) name. Without it the raw URL renders as an autolink.
    :return: A markdown string with the metadata block.
    """
    sha = md.sha or ""
    actor = md.event_actor or ""
    triggering_actor = md.triggering_actor
    rerun_entry = ""
    if triggering_actor and triggering_actor != actor:
        rerun_entry = f"- **Re-run by**: @{triggering_actor}\n"
    docs_entry = ""
    if docs_url:
        link = f"[`{docs_name}`]({docs_url})" if docs_name else f"<{docs_url}>"
        docs_entry = f"- **Documentation**: {link}\n"

    return render_template(
        "pr-metadata",
        docs_entry=docs_entry,
        event_name=md.event_name,
        actor=actor,
        rerun_entry=rerun_entry,
        ref_name=md.ref_name,
        repo_url=md.repo_url,
        sha=sha,
        sha_short=sha[:8],
        job=md.job_name,
        workflow_file=extract_workflow_filename(md.workflow_ref),
        run_id=md.run_id,
        run_number=md.run_number,
        run_attempt=md.run_attempt,
    )


def generate_refresh_tip(md: Metadata) -> str:
    """Generate a tip admonition inviting users to refresh the PR manually.

    Reads the repository URL and `GITHUB_WORKFLOW_REF` from *md* to build
    the workflow dispatch URL.

    :param md: The {class}`~repomatic.metadata.core.Metadata` instance to read
        CI context from.
    :return: A GitHub-flavored markdown `[!TIP]` blockquote, or an empty
        string if the workflow reference is unavailable.
    """
    workflow_file = extract_workflow_filename(md.workflow_ref)
    if not workflow_file:
        return ""
    return render_template(
        "refresh-tip",
        repo_url=md.repo_url,
        workflow_file=workflow_file,
    )


def _utf16_len(text: str) -> int:
    """Length of *text* in UTF-16 code units.

    GitHub's body size limit counts JavaScript string length (UTF-16 code
    units), not Unicode code points, so emoji-heavy reports must be measured
    the same way.
    """
    return len(text.encode("utf-16-le")) // 2


def _trim_to_budget(text: str, budget: int) -> str:
    """Keep the leading whole lines of *text* that fit in *budget*.

    {func}`~repomatic.github.outputs.trim_to_budget` in the body-content unit,
    UTF-16 code units.

    :param text: Content to trim.
    :param budget: Available room, in UTF-16 code units.
    :return: The kept lines, right-stripped; empty when nothing fits.
    """
    return trim_to_budget(text, budget, _utf16_len)


def build_pr_body(prefix: str, metadata_block: str, refresh_tip: str = "") -> str:
    """Concatenate prefix, refresh tip, and metadata block into a PR body.

    The `metadata_block` already includes the attribution footer (appended
    automatically by {func}`~repomatic.templating.render_template`); the
    *refresh_tip* comes pre-rendered from {func}`generate_refresh_tip` (empty
    to omit it).

    Bodies over {data}`GITHUB_BODY_MAX_CHARS` have their prefix trimmed to
    fit, replacing the dropped lines with a caution admonition, so the refresh
    tip, metadata block, and attribution footer always survive. Left alone,
    GitHub-side truncation would chop the body from the end instead.

    :param prefix: Content to prepend before the metadata block. Can be empty.
    :param metadata_block: The collapsible metadata block from
        {func}`generate_pr_metadata_block`, with footer.
    :param refresh_tip: Pre-rendered refresh-tip admonition, or empty.
    :return: The complete PR body string.
    """
    parts: list[str] = []
    if prefix:
        parts.append(prefix)
    if refresh_tip:
        parts.append(refresh_tip)
    parts.append(metadata_block)
    body = "\n\n\n".join(parts)
    if not prefix or _utf16_len(body) <= GITHUB_BODY_MAX_CHARS:
        return body

    # Rebuild with the prefix cut down to whatever budget the fixed tail
    # leaves.
    tail = "\n\n\n".join([_TRUNCATION_NOTICE, *parts[1:]])
    budget = GITHUB_BODY_MAX_CHARS - _utf16_len(tail) - len("\n\n\n")
    truncated_prefix = _trim_to_budget(prefix, budget)
    if not truncated_prefix:
        return tail
    return f"{truncated_prefix}\n\n\n{tail}"


def fit_github_body(body: str) -> str:
    """Trim an oversized issue or pull-request body, keeping the footer.

    The {func}`build_pr_body` counterpart for bodies rendered straight from a
    footer-carrying template (broken-links report, setup guide), and the
    safety net {func}`~repomatic.github.pr.upsert_pr` runs over an explicit
    `--body` that never went through {func}`build_pr_body`. The GitHub API
    rejects oversized bodies outright (`gh issue create`, `gh issue edit` and
    `gh pr create` all fail), so the content above the attribution footer is
    trimmed on line boundaries, with a caution admonition marking the cut.

    :param body: The rendered body, attribution footer included.
    :return: The body unchanged when it fits, else trimmed to fit.
    """
    if _utf16_len(body) <= GITHUB_BODY_MAX_CHARS:
        return body
    footer_suffix = (
        "\n\n---\n\n" + render_template("generated-footer", version=__version__) + "\n"
    )
    if body.endswith(footer_suffix):
        content = body.removesuffix(footer_suffix)
        tail = f"\n\n{_TRUNCATION_NOTICE}{footer_suffix}"
    else:
        # No recognizable footer (external or hand-built body): still trim to
        # fit, without resurrecting a footer that was never there.
        content = body
        tail = f"\n\n{_TRUNCATION_NOTICE}\n"
    trimmed = _trim_to_budget(content, GITHUB_BODY_MAX_CHARS - _utf16_len(tail))
    if not trimmed:
        return tail.lstrip()
    return f"{trimmed}{tail}"


@contextmanager
def temp_body_file(body: str) -> Generator[Path, None, None]:
    """Materialize a rendered body as a temporary file, then remove it.

    The `gh` CLI takes a body only through `--body-file`, so every write path
    against an issue or a pull request needs one on disk. Owning the temp file
    at this layer keeps callers working in the currency they actually produce
    (rendered markdown) instead of each repeating the same write / `try` /
    `unlink` envelope.
    """
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".md",
        delete=False,
        encoding="UTF-8",
    ) as handle:
        handle.write(body)
        path = Path(handle.name)
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def _arg_sources(
    md: Metadata,
    config: Config,
    *,
    version: str | None,
    part: str | None,
    pr_ref: str | None,
) -> dict[str, str | Callable[[], str | None] | None]:
    """Where each template argument repomatic knows comes from.

    A callable is only called when the template declares its argument, so a
    template pays for no lookup it does not use.

    :param md: CI context, read for the repository and the commit.
    :param config: The resolved `[tool.repomatic]` configuration.
    :param version: The `--version` value, or `None` to read the bumpversion
        configuration.
    :param part: The `--part` value.
    :param pr_ref: The `--pr-ref` value.
    :return: Argument name to its value, a callable producing it, or `None`
        when nothing supplies it.
    """

    def resolve_version() -> str:
        """The release version, `--version` first, else the bumpversion config."""
        if version is not None:
            return version
        current = md.get_current_version()
        if not current:
            msg = "Cannot auto-detect version: no bumpversion config found."
            raise TemplateArgumentError(msg)
        resolved = strip_dev_suffix(current)
        logging.info(f"Auto-detected version: {resolved}")
        return resolved

    # The two review steps of the prepare-release checklist share one GitHub
    # releases lookup, made only when the template declares them.
    review_steps: dict[str, str] = {}

    def review_step(key: str) -> str:
        if not review_steps:
            dev_release_review, changes_review = release_review_steps(
                md.repo_url, resolve_version()
            )
            review_steps["dev_release_review"] = dev_release_review
            review_steps["changes_review"] = changes_review
        return review_steps[key]

    def release_readiness() -> str:
        # Pinned to the commit the body was rendered from, so the line the
        # banner points at is the line that was read. A branch ref would drift
        # onto whatever `main` holds when the maintainer clicks it.
        blob_url = f"{md.repo_url}/blob/{md.sha}" if md.repo_url and md.sha else None
        return build_release_readiness(
            Path("pyproject.toml"),
            Path("uv.lock"),
            config.minimum_release_age,
            allow=config.lint_deps.allow,
            source_url=blob_url,
            repo_url=md.repo_url or None,
        )

    return {
        "changes_review": lambda: review_step("changes_review"),
        "dev_release_review": lambda: review_step("dev_release_review"),
        # File-backed step outputs, relayed by the workflow step that opens
        # the pull request.
        "diff_table": lambda: read_file_output("REPOMATIC_DIFF_TABLE"),
        "part": part,
        "pr_ref": pr_ref,
        "release_readiness": release_readiness,
        "repo_url": lambda: md.repo_url,
        "upgrade_report": lambda: read_file_output("REPOMATIC_UPGRADE_REPORT"),
        "upgrade_version": os.getenv("REPOMATIC_UPGRADE_VERSION"),
        "version": resolve_version,
    }


def render_pr(
    md: Metadata,
    config: Config,
    *,
    template: PrTemplate | None = None,
    prefix: str = "",
    extra_args: Mapping[str, str] | None = None,
    version: str | None = None,
    part: str | None = None,
    pr_ref: str | None = None,
) -> PrContent:
    """Render a pull request's title, body and commit message.

    The rendering core shared by `pr-body` (which emits the three as step
    outputs for external consumption) and `pr-sync` (which feeds them straight
    to {func}`~repomatic.github.pr.upsert_pr`, skipping the `$GITHUB_OUTPUT`
    round-trip and its step-output ceiling entirely).

    :param md: CI context. One instance for the whole render, so the review
        steps, the `repo_url` argument and the metadata block read the same
        state.
    :param config: The resolved `[tool.repomatic]` configuration.
    :param template: The template to render, or `None` for a body made of
        *prefix* and the metadata block alone.
    :param prefix: Content placed before the rendered template.
    :param extra_args: Template arguments given explicitly. They win over every
        source repomatic knows, so a caller can feed any name.
    :param version: The release version, read from the bumpversion
        configuration when `None`.
    :param part: The version part a bump template names.
    :param pr_ref: The pull request reference a squash-merge report names.
    :return: The title and commit message are empty when no template supplies
        them.
    :raises TemplateArgumentError: When nothing supplies an argument the
        template declares.
    """
    title = commit_message = docs_url = docs_name = ""
    if template:
        sources = _arg_sources(md, config, version=version, part=part, pr_ref=pr_ref)
        sources.update(extra_args or {})
        kwargs: dict[str, str | None] = {}
        for arg in template.args:
            value = sources.get(arg)
            if value is None:
                msg = f"--{arg} is required for template {template.name!r}"
                raise TemplateArgumentError(msg)
            kwargs[arg] = value() if callable(value) else value
        content = render_template(template, **kwargs)
        prefix = f"{prefix}\n\n{content}" if prefix else content
        title = template.title(**kwargs)
        commit_message = template.commit_message(**kwargs)
        docs_url, docs_name = template.docs_url, template.name

    metadata_block = generate_pr_metadata_block(
        md, docs_url=docs_url, docs_name=docs_name
    )
    body = build_pr_body(prefix, metadata_block, refresh_tip=generate_refresh_tip(md))
    return PrContent(title, body, commit_message)


# 8.0.0 shim: re-export for callers not migrated yet.
def load_template(name: str | Path) -> tuple[dict[str, object], str]:
    """Load a template's frontmatter and body.

    See {meth}`repomatic.templating.PrTemplate.load`.
    """
    template = PrTemplate.load(name)
    return dict(template.meta), template.body


# 8.0.0 shim: re-export for callers not migrated yet.
def template_docs_url(name: str | Path) -> str:
    """A template's documentation link.

    See {attr}`repomatic.templating.PrTemplate.docs_url`.
    """
    return PrTemplate.load(name).docs_url


# 8.0.0 shim: re-export for callers not migrated yet.
def template_labels(name: str | Path) -> list[str]:
    """A template's pull request labels.

    See {attr}`repomatic.templating.PrTemplate.labels`.
    """
    return list(PrTemplate.load(name).labels)
