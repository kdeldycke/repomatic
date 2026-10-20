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

"""Load and render the markdown templates of `repomatic/templates/`.

A template is a markdown body with optional YAML frontmatter. The body uses
`string.Template` placeholders (`$variable`), which leave markdown braces like
`[tool.repomatic]` alone. The frontmatter carries what an operation needs
besides the body: the `args` the body takes, and, for a template that fronts a
pull request, its `title`, `commit_message`, `labels`, `draft` state and `docs`
link.

{class}`PrTemplate` parses a template once and answers every question about
it. {func}`render_template` renders one or more templates into one document,
with the attribution footer.

```{note}
A template is also read from a {class}`~pathlib.Path`. Downstream repos ship
project-specific templates and feed them through `repomatic pr-sync
--template-file path/to/template.md` (with one or more `--template-arg
KEY=VALUE` entries to fill the placeholders) without forking repomatic. Such a
template sets `footer: false` in its frontmatter: the metadata block of a pull
request body already carries the attribution footer.
```
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from importlib.resources import as_file, files
from pathlib import Path
from string import Template
from types import MappingProxyType

from . import __version__
from .frontmatter import split_frontmatter

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Mapping


TEMPLATE_PACKAGE = "repomatic.templates"
"""The package holding the bundled templates."""

TEMPLATE_SUFFIXES = (".md.noformat", ".md")
"""Extensions of a template file, in lookup order.

`.md.noformat` hides a template from mdformat, whose list parser reads a
placeholder at the head of a list line (`$rerun_entry- **Ref**`) as literal
text and breaks the list. `pr-metadata.md.noformat` is the canonical example.
"""

FOOTER_TEMPLATE = "generated-footer"
"""The template holding the attribution footer {func}`render_template` appends."""


def _unescape_dollars(text: str) -> str:
    r"""Replace `\$` with `$` in template text.

    ```{caution}
    Workaround for mdformat escaping `$` characters in markdown files.
    Templates use `string.Template` (`$variable` syntax), but mdformat
    rewrites `$var` as `\$var`. This undoes it at load time so that
    `string.Template.substitute()` sees the original placeholders.
    ```
    """
    return text.replace(r"\$", "$")


def _parse_template(raw: str) -> tuple[dict[str, object], str]:
    """Split a template file into YAML frontmatter and markdown body.

    Delegates the split to {func}`~repomatic.frontmatter.split_frontmatter` and
    adds the one template-specific step: restoring the `$placeholder` syntax
    mdformat escapes (see {func}`_unescape_dollars`). The body is unescaped, the
    frontmatter is not: its values are metadata (`args`, `title`, `docs`), never
    `string.Template` sources.

    :param raw: Raw template file content.
    :return: A tuple of (frontmatter dict, body string).
    """
    meta, body = split_frontmatter(raw)
    return meta, _unescape_dollars(body)


def _substitute(text: str, kwargs: Mapping[str, str | None]) -> str:
    """Apply `string.Template` substitution if kwargs are provided.

    `None` values are normalized to empty strings so callers can pass
    {class}`~repomatic.metadata.core.Metadata` properties directly without
    coercing at the call site.
    """
    if kwargs:
        safe = {k: (v if v is not None else "") for k, v in kwargs.items()}
        return Template(text).substitute(safe)
    return text


def footer_opted_out(meta: Mapping[str, object]) -> bool:
    """Whether a template's frontmatter opts out of the attribution footer.

    A template opts out with `footer: false`. Both the boolean and the quoted
    string are honored: the frontmatter is YAML, so a bare `false` parses as a
    boolean, but a downstream template may well have quoted it, and the two
    spellings mean the same thing to whoever wrote them. An absent field keeps
    the footer. {func}`repomatic.lint_repo.check_pr_templates` applies the same
    rule, so it never flags a spelling the renderer honors.

    :param meta: Parsed frontmatter of one template.
    :return: `True` when the template drops the footer.
    """
    return meta.get("footer", True) in (False, "false")


def template_stem(filename: str) -> str:
    """Return a template's name, shorn of its `.md` or `.md.noformat` extension.

    The one place that knows how template filenames decompose: `.md.noformat`
    files are renamed `.md` files hidden from mdformat (see
    {data}`TEMPLATE_SUFFIXES`), so both extensions strip down to the same name.

    :param filename: A template file's basename.
    :return: The name with neither extension.
    """
    return filename.removesuffix(".noformat").removesuffix(".md")


def get_template_names() -> list[str]:
    """Discover all available template names from the templates package.

    :return: Sorted list of template names (without extension).
    """
    return sorted(
        template_stem(item.name)
        for item in files(TEMPLATE_PACKAGE).iterdir()
        if item.name.endswith(TEMPLATE_SUFFIXES)
    )


@dataclass(frozen=True)
class PrTemplate:
    """One template, parsed once: its frontmatter and its body.

    Every question about a template reads the same parse: the bundled ones are
    loaded once per process by {meth}`load`, and one pull request sync asks a
    template for its title, commit message, labels, draft state, docs link and
    body.
    """

    name: str
    """The template's name: the bundled name, or the stem of an external file.

    Also the default branch of the pull request it fronts, and the label of the
    documentation link in its metadata block.
    """

    meta: Mapping[str, object]
    """The parsed frontmatter, read-only."""

    body: str
    """The markdown body, with mdformat's `$` escapes undone."""

    @classmethod
    def load(cls, ref: str | Path) -> PrTemplate:
        """Load a template by bundled name or by filesystem path.

        A `str` (like `"bump-version"`) names a bundled template, looked up
        under each of {data}`TEMPLATE_SUFFIXES` in turn and cached for the
        process. A {class}`~pathlib.Path` is read from disk on every call, so a
        downstream repo iterating on its own template sees each edit.

        :param ref: Template name without extension, or a template file path.
        :raises FileNotFoundError: If neither resource nor file exists.
        """
        if isinstance(ref, Path):
            if not ref.is_file():
                msg = f"Template file {str(ref)!r} does not exist"
                raise FileNotFoundError(msg)
            meta, body = _parse_template(ref.read_text(encoding="UTF-8"))
            return cls(template_stem(ref.name), MappingProxyType(meta), body)
        return _load_bundled(ref)

    @property
    def args(self) -> tuple[str, ...]:
        """The placeholder names the frontmatter `args` field declares."""
        args = self.meta.get("args", [])
        return tuple(map(str, args)) if isinstance(args, list) else ()

    @property
    def labels(self) -> tuple[str, ...]:
        """The labels the template's pull request carries.

        Read from the `labels:` frontmatter key, accepting a YAML list or a
        single string. The template is the one place that knows which operation
        it fronts, so its labels live beside its title and commit message
        rather than in every workflow step that opens the pull request.
        """
        labels = self.meta.get("labels", [])
        if isinstance(labels, str):
            return (labels,)
        if isinstance(labels, list):
            return tuple(map(str, labels))
        return ()

    @property
    def draft(self) -> bool:
        """Whether the template's pull request is held in draft.

        Both the YAML boolean and its quoted spelling are honored, mirroring
        {func}`footer_opted_out`.
        """
        return self.meta.get("draft", False) in (True, "true")

    @property
    def docs_url(self) -> str:
        """The template's documentation deep link, empty when it declares none.

        A pull request template carries a `docs:` field pointing at its job's
        section of the hosted workflows reference, surfaced as the
        `Documentation` entry of the metadata block.
        """
        docs = self.meta.get("docs", "")
        return docs if isinstance(docs, str) else ""

    @property
    def wants_footer(self) -> bool:
        """Whether rendering appends the attribution footer.

        See {func}`footer_opted_out` for the frontmatter that drops it.
        """
        return not footer_opted_out(self.meta)

    def title(self, **kwargs: str | None) -> str:
        """Render the frontmatter `title`, empty when the template has none.

        :param kwargs: Variables to substitute into the title.
        """
        title = self.meta.get("title")
        if not title or not isinstance(title, str):
            return ""
        return _substitute(title, kwargs)

    def commit_message(self, **kwargs: str | None) -> str:
        """Render the frontmatter `commit_message`, falling back to the title.

        Empty when the template defines neither: a template without them only
        renders a body.

        :param kwargs: Variables to substitute into the commit message.
        """
        message = self.meta.get("commit_message")
        if message and isinstance(message, str):
            return _substitute(message, kwargs)
        return self.title(**kwargs)

    def render_body(self, **kwargs: str | None) -> str:
        """Render the body alone, stripped, without the attribution footer.

        :param kwargs: Variables to substitute into the body.
        """
        return _substitute(self.body, kwargs).strip()


@cache
def _load_bundled(name: str) -> PrTemplate:
    """Load and parse a bundled template, once per process.

    Bundled resources are immutable for the life of the interpreter, so the
    parse is memoized.

    :param name: Template name without extension.
    :raises FileNotFoundError: If no such bundled template exists.
    """
    template_files = files(TEMPLATE_PACKAGE)
    for suffix in TEMPLATE_SUFFIXES:
        resource = template_files.joinpath(f"{name}{suffix}")
        if resource.is_file():
            with as_file(resource) as path:
                raw = path.read_text(encoding="UTF-8")
            meta, body = _parse_template(raw)
            return PrTemplate(name, MappingProxyType(meta), body)
    msg = f"Template {name!r} not found in repomatic/templates/"
    raise FileNotFoundError(msg)


def render_template(*refs: str | Path | PrTemplate, **kwargs: str | None) -> str:
    """Load and render one or more templates with variable substitution.

    When several templates are given, each is rendered and joined with a blank
    line. The `generated-footer` attribution is appended once at the end if
    **any** of the templates wants it (see {attr}`PrTemplate.wants_footer`).

    Consecutive blank lines left by empty variables are collapsed to a single
    blank line.

    ```{note}
    The footer's version is `__version__` as read from the working tree, which
    makes it cosmetically wrong on the version-machinery PRs. That is accepted
    rather than fixed. Upstream runs the CLI from its own checkout
    ({data}`~repomatic.release.prepare_release.LOCAL_CLI_INVOCATION`), and both the
    `bump-version` and `prepare-release` jobs rewrite `__version__` with
    `bump-my-version` before this renders, so each of those bodies advertises
    the version its own PR produces (the `minor` or `major` bump target, or
    the post-release patch bump) instead of the identical code that rendered
    it. Both fixes cost more than the tag is worth: rendering the body before
    the bump means splitting `pr-body` back out of `pr-sync`, and feeding the
    pre-bump version in means a CLI option that exists only to work around
    step ordering. Downstream never sees it, since a frozen workflow runs
    `uvx 'repomatic==X.Y.Z'` and takes its version from the installed
    distribution.
    ```

    :param refs: Template names (without extension), template file paths, or
        templates already loaded.
    :param kwargs: Variables to substitute into all templates.
    :return: The rendered markdown string.
    """
    templates = [
        ref if isinstance(ref, PrTemplate) else PrTemplate.load(ref) for ref in refs
    ]
    result = "\n\n".join(template.render_body(**kwargs) for template in templates)
    append_footer = any(template.wants_footer for template in templates)
    if append_footer:
        footer = PrTemplate.load(FOOTER_TEMPLATE).render_body(version=__version__)
        result += f"\n\n---\n\n{footer}"
    result = re.sub(r"\n{3,}", "\n\n", result)
    if append_footer:
        result += "\n"
    return result
