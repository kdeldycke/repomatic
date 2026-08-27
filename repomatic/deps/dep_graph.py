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
"""Generate Mermaid dependency graphs from uv lockfiles.

Every box in the graph (the primary dependencies rectangle and each
`--group`/`--extra` subgraph) only holds directly-declared dependencies,
drawn as hexagons: the packages under the project's control, referenced in
`pyproject.toml`. Transitive dependencies always render outside the boxes,
as plain ovals.

```{note}
Uses `uv export --format cyclonedx1.5` which provides structured JSON
with dependency relationships, replacing the need for pipdeptree.
```

```{warning}
The generated Mermaid syntax targets the version bundled with
`sphinxcontrib-mermaid`, currently `11.12.1`. See the hard-coded
`MERMAID_VERSION` constant in [sphinxcontrib-mermaid's source](https://github.com/mgaitan/sphinxcontrib-mermaid/blob/master/sphinxcontrib/mermaid/__init__.py).
Avoid using Mermaid features introduced after that version.
```
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from pathlib import Path

from ..pypi import PYPI_PACKAGE_URL
from ..pyproject import read_pyproject_toml
from .uv import load_lock_data, parse_lock_specifiers, uv_cmd

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import Any

    from .uv import LockSpecifiers


@lru_cache(maxsize=16)
def _get_cyclonedx_sbom_cached(
    package: str | None = None,
    groups: tuple[str, ...] | None = None,
    extras: tuple[str, ...] | None = None,
    frozen: bool = True,
) -> str:
    """Cached wrapper around uv export command.

    Returns the raw JSON string to allow caching (dicts are not hashable).
    """
    cmd = uv_cmd("export", frozen=frozen)
    cmd.extend([
        "--format",
        "cyclonedx1.5",
        "--no-hashes",
        "--preview-features",
        "sbom-export",
    ])
    if package:
        cmd.extend(["--package", package])
    if groups:
        for group in groups:
            cmd.extend(["--group", group])
    if extras:
        for extra in extras:
            cmd.extend(["--extra", extra])

    logging.debug(f"Running: {' '.join(cmd)}")
    # The SBOM carries package descriptions and author names, so the decoding
    # is pinned: `text=True` alone would fall back to the platform default and
    # raise UnicodeDecodeError on Windows.
    result = subprocess.run(
        cmd, capture_output=True, text=True, encoding="UTF-8", check=True
    )
    return result.stdout


STYLE_PRIMARY_DEPS_SUBGRAPH: str = "fill:#1565C020,stroke:#42A5F5"
"""Mermaid style for the primary dependencies subgraph box.

Uses semi-transparent fill (8-digit hex) so the tint adapts to both
light and dark page backgrounds.
"""

STYLE_EXTRA_SUBGRAPH: str = "fill:#7B1FA220,stroke:#BA68C8"
"""Mermaid style for extra dependency subgraph boxes.

Uses semi-transparent fill (8-digit hex) so the tint adapts to both
light and dark page backgrounds.
"""

STYLE_GROUP_SUBGRAPH: str = "fill:#546E7A20,stroke:#90A4AE"
"""Mermaid style for group dependency subgraph boxes.

Uses semi-transparent fill (8-digit hex) so the tint adapts to both
light and dark page backgrounds.
"""

STYLE_PRIMARY_NODE: str = "stroke-width:3px"
"""Mermaid style for root and primary dependency nodes (thick border)."""

STYLE_DUPLICATE_NODE: str = f"{STYLE_PRIMARY_NODE},stroke-dasharray:5 5"
"""Mermaid style for duplicate headline nodes (dashed thick border).

The dashes mark the node as a display-only mirror of the real node owned by
another subgraph; a dotted identity link ties the two together. Derived from
{data}`STYLE_PRIMARY_NODE` since duplicates are always headline (primary)
dependencies of their box.
"""


class SubgraphKind(Enum):
    """Kind of dependency selector a subgraph box represents."""

    GROUP = "group"
    EXTRA = "extra"

    @property
    def flag(self) -> str:
        """CLI flag selecting this kind, shown as the box title prefix."""
        return f"--{self.value}"

    def available(self, project_root: Path | None = None) -> tuple[str, ...]:
        """Discover this kind's declared names from `pyproject.toml`.

        Groups come from the `[dependency-groups]` table, extras from
        `[project.optional-dependencies]`.

        :param project_root: Directory holding `pyproject.toml`. Defaults to
            the current working directory.
        :return: Sorted tuple of group or extra names.
        """
        data = read_pyproject_toml(project_root)
        if self is SubgraphKind.GROUP:
            table = data.get("dependency-groups", {})
        else:
            table = data.get("project", {}).get("optional-dependencies", {})
        return tuple(sorted(table))

    @property
    def mermaid_prefix(self) -> str:
        """Namespace prefix keeping subgraph IDs distinct from node IDs.

        Without it, a `json5` extra box would collide with a `json5` package
        node.
        """
        return "grp" if self is SubgraphKind.GROUP else "ext"

    @property
    def style(self) -> str:
        """Mermaid style for boxes of this kind."""
        if self is SubgraphKind.GROUP:
            return STYLE_GROUP_SUBGRAPH
        return STYLE_EXTRA_SUBGRAPH


@dataclass
class Subgraph:
    """One `--group` or `--extra` box in the rendered graph.

    A box only holds the packages its group or extra declares directly: the
    dependencies under the project's control, referenced in `pyproject.toml`.
    Transitive dependencies always render outside the boxes, exactly like the
    transitive dependencies of the primary set.
    """

    kind: SubgraphKind
    """Whether the box represents a dependency group or an optional extra."""

    name: str
    """Group or extra name, as declared in `pyproject.toml`."""

    owned: set[str]
    """Directly-declared packages this box renders as real hexagon nodes."""

    duplicates: set[str]
    """Directly-declared packages owned by a sibling box.

    Rendered as display-only duplicate nodes tied to the real node by a dotted
    identity link. See {func}`attribute_subgraph_packages`.
    """

    @property
    def mermaid_id(self) -> str:
        """Mermaid subgraph ID, namespaced away from node IDs."""
        return f"{self.kind.mermaid_prefix}_{self.name}"

    @property
    def title(self) -> str:
        """Box title, echoing the CLI flag that pulls these packages in."""
        return f"{self.kind.flag} {self.name}"


MERMAID_RESERVED_KEYWORDS: frozenset[str] = frozenset((
    "C4Component",
    "C4Container",
    "C4Deployment",
    "C4Dynamic",
    "_blank",
    "_parent",
    "_self",
    "_top",
    "call",
    "class",
    "classDef",
    "click",
    "end",
    "flowchart",
    "flowchart-v2",
    "graph",
    "interpolate",
    "linkStyle",
    "style",
    "subgraph",
))
"""Mermaid keywords that cannot be used as node IDs.

```{seealso}
https://github.com/mermaid-js/mermaid/issues/4182#issuecomment-1454787806
https://github.com/tox-dev/pipdeptree/pull/201
```
"""


def normalize_package_name(name: str) -> str:
    """Normalize package name for use as Mermaid node ID.

    Converts to lowercase and replaces non-alphanumeric characters with underscores.
    Appends `_0` suffix to avoid conflicts with Mermaid reserved keywords.
    """
    node_id = re.sub(r"[^a-z0-9]", "_", name.lower())
    if node_id in MERMAID_RESERVED_KEYWORDS:
        node_id = f"{node_id}_0"
    return node_id


def resolve_subgraph_selection(
    kind: SubgraphKind,
    explicit: tuple[str, ...],
    select_all: bool,
    excluded: tuple[str, ...],
    only: tuple[str, ...],
    config_all: bool,
    config_excluded: Sequence[str],
) -> tuple[str, ...] | None:
    """Resolve which groups or extras the graph should render.

    Mirrors one selection axis of the `update-dep-graph` command: explicit
    CLI values win over the `[tool.repomatic] dependency-graph` defaults;
    `--only-*` replaces the explicit selection; `--all-*` expands to every
    name declared in `pyproject.toml`; `--no-*` prunes last.

    :param kind: The axis to resolve, groups or extras.
    :param explicit: Names selected one by one (`--group`/`--extra`).
    :param select_all: Select every declared name (`--all-groups`/`--all-extras`).
    :param excluded: Names to prune from the selection (`--no-group`/`--no-extra`).
    :param only: Names selected in exclusive mode (`--only-group`/`--only-extra`).
    :param config_all: Configured default for *select_all*, applied when no
        selection flag is passed.
    :param config_excluded: Configured default for *excluded*.
    :return: Selected names, or `None` when the axis is not requested at all.
    """
    if not select_all and not explicit and not only:
        select_all = config_all
    if not excluded:
        excluded = tuple(config_excluded)
    if only:
        explicit = only
    resolved = explicit if explicit else None
    if select_all:
        resolved = kind.available()
        logging.info(f"Discovered {kind.value}s: {', '.join(resolved)}")
    if excluded and resolved:
        resolved = tuple(name for name in resolved if name not in excluded)
        logging.info(f"After exclusions, {kind.value}s: {', '.join(resolved)}")
    return resolved


def get_cyclonedx_sbom(
    package: str | None = None,
    groups: tuple[str, ...] | None = None,
    extras: tuple[str, ...] | None = None,
    frozen: bool = True,
) -> dict[str, Any]:
    """Run uv export and return the CycloneDX SBOM as a dictionary.

    Results are cached to avoid redundant subprocess calls within the same process.

    :param package: Optional package name to focus the export on.
    :param groups: Optional dependency groups to include (e.g., "test", "typing").
    :param extras: Optional extras to include (e.g., "xml", "json5").
    :param frozen: If True, use --frozen to skip lock file updates.
    :return: Parsed CycloneDX SBOM dictionary.
    :raises subprocess.CalledProcessError: If uv command fails.
    :raises json.JSONDecodeError: If output is not valid JSON.
    """
    raw_json = _get_cyclonedx_sbom_cached(
        package=package, groups=groups, extras=extras, frozen=frozen
    )
    sbom: dict[str, Any] = json.loads(raw_json)
    return sbom


def get_package_names_from_sbom(sbom: dict[str, Any]) -> set[str]:
    """Extract all package names from a CycloneDX SBOM.

    :param sbom: Parsed CycloneDX SBOM dictionary.
    :return: Set of package names.
    """
    names: set[str] = set()
    # Add root component.
    metadata = sbom.get("metadata", {})
    root_component = metadata.get("component", {})
    if root_name := root_component.get("name"):
        names.add(root_name)
    # Add all components.
    for component in sbom.get("components", []):
        if name := component.get("name"):
            names.add(name)
    return names


def build_dependency_graph(
    sbom: dict[str, Any],
) -> tuple[str, set[str], list[tuple[str, str]]]:
    """Build a dependency graph from CycloneDX SBOM data.

    :param sbom: Parsed CycloneDX SBOM dictionary.
    :return: Tuple of (root_name, package_names, edges_list) where:
        - root_name is the root package name
        - package_names is the set of all package names
        - edges_list is a list of (from_name, to_name) tuples
    """
    # Map each bom-ref to its package name to resolve dependency edges.
    ref_names: dict[str, str] = {}

    metadata = sbom.get("metadata", {})
    root_component = metadata.get("component", {})
    root_ref = root_component.get("bom-ref", "")
    root_name = root_component.get("name", "")
    if root_ref:
        ref_names[root_ref] = root_name

    for component in sbom.get("components", []):
        bom_ref = component.get("bom-ref", "")
        name = component.get("name", "")
        if bom_ref and name:
            ref_names[bom_ref] = name

    edges: list[tuple[str, str]] = []
    for dep in sbom.get("dependencies", []):
        from_ref = dep.get("ref", "")
        if from_ref not in ref_names:
            continue
        from_name = ref_names[from_ref]
        for to_ref in dep.get("dependsOn", []):
            if to_ref in ref_names:
                edges.append((from_name, ref_names[to_ref]))

    return root_name, set(ref_names.values()), edges


def filter_root_edges(
    root_name: str,
    edges: list[tuple[str, str]],
    main_deps: set[str] | None,
    subgraphs: Sequence[Subgraph],
) -> list[tuple[str, str]]:
    """Drop root edges that no `pyproject.toml` declaration backs.

    uv's CycloneDX export hangs a dependency-group package off the root as soon
    as that package lands in the resolved component set, whether or not the
    group was requested. Exporting click-extra with `--extra sphinx` and no
    `--group` is enough for `requests` to come back as a direct dependency of
    the project: Sphinx pulls it in, the `test` group happens to declare it too,
    and the export conflates the two. Neither omitting `--group` nor passing
    `--no-default-groups` suppresses it.

    Left in place, such an edge lands the package in the primary dependencies
    box, labelled with the specifier of a group nobody asked for, claiming the
    project depends on something a plain install never installs. So the root's
    direct dependencies are re-derived from `uv.lock`, which records what
    `pyproject.toml` declares rather than what resolution happened to produce.

    Edges into a box-owned package survive: {func}`render_mermaid` turns those
    into the box's dashed arrow. Edges that do not start at the root are never
    touched, so the dropped package keeps rendering as a transitive dependency
    of whatever actually pulls it in.

    :param root_name: The root package name.
    :param edges: List of (from_name, to_name) edge tuples.
    :param main_deps: Names the root declares as main dependencies, from
        {attr}`~repomatic.deps.uv.LockSpecifiers.by_main`. `None` when the lockfile
        describes no such package, in which case every edge is kept: missing
        data is not evidence that an edge is spurious.
    :param subgraphs: Boxes whose owned packages legitimately hang off the root.
    :return: The edge list, without the unbacked root edges.
    """
    if main_deps is None:
        return edges
    allowed = set(main_deps)
    for subgraph in subgraphs:
        allowed |= subgraph.owned
    return [
        (from_name, to_name)
        for from_name, to_name in edges
        if from_name != root_name or to_name in allowed
    ]


def filter_graph_to_package(
    packages: set[str],
    edges: list[tuple[str, str]],
    package: str,
) -> tuple[set[str], list[tuple[str, str]]]:
    """Filter the graph to only include dependencies of a specific package.

    :param packages: Set of all package names.
    :param edges: List of (from_name, to_name) edge tuples.
    :param package: Package name to filter to.
    :return: Filtered (packages, edges) tuple.
    """
    # Find all packages reachable from the target package.
    reachable: set[str] = {package}
    changed = True
    while changed:
        changed = False
        for from_name, to_name in edges:
            if from_name in reachable and to_name not in reachable:
                reachable.add(to_name)
                changed = True

    # Filter edges.
    filtered_edges = [
        (from_name, to_name)
        for from_name, to_name in edges
        if from_name in reachable and to_name in reachable
    ]

    # Keep only the target and the packages that appear in surviving edges.
    used_names = {package}
    for from_name, to_name in filtered_edges:
        used_names.add(from_name)
        used_names.add(to_name)

    return packages & used_names, filtered_edges


def trim_graph_to_depth(
    root_name: str,
    packages: set[str],
    edges: list[tuple[str, str]],
    depth: int,
) -> tuple[set[str], list[tuple[str, str]]]:
    """Trim the graph to only include nodes within a given depth from the root.

    Performs a breadth-first traversal from the root, keeping only nodes
    reachable within `depth` hops and edges between those nodes.

    :param root_name: The root package name.
    :param packages: Set of all package names.
    :param edges: List of (from_name, to_name) edge tuples.
    :param depth: Maximum depth from root. 0 = root only, 1 = root + primary deps, etc.
    :return: Filtered (packages, edges) tuple.
    """
    # Build adjacency list for BFS.
    adjacency: dict[str, list[str]] = {}
    for from_name, to_name in edges:
        adjacency.setdefault(from_name, []).append(to_name)

    # BFS traversal.
    reachable: set[str] = {root_name}
    frontier: set[str] = {root_name}
    for _ in range(depth):
        next_frontier: set[str] = set()
        for name in frontier:
            for neighbor in adjacency.get(name, []):
                if neighbor not in reachable:
                    reachable.add(neighbor)
                    next_frontier.add(neighbor)
        frontier = next_frontier
        if not frontier:
            break

    # Filter edges to only those between reachable nodes.
    filtered_edges = [
        (from_name, to_name)
        for from_name, to_name in edges
        if from_name in reachable and to_name in reachable
    ]

    return packages & reachable, filtered_edges


def _compute_node_degrees(edges: list[tuple[str, str]]) -> dict[str, int]:
    """Compute total degree (in + out) for each node in the edge list.

    Nodes with more connections are more central to the graph and benefit
    from being declared earlier, which helps dagre allocate better positions.

    :param edges: List of (from_name, to_name) edge tuples.
    :return: Dict mapping node name to total degree.
    """
    degrees: dict[str, int] = {}
    for from_name, to_name in edges:
        degrees[from_name] = degrees.get(from_name, 0) + 1
        degrees[to_name] = degrees.get(to_name, 0) + 1
    return degrees


def _compute_subtree_sizes(edges: list[tuple[str, str]]) -> dict[str, int]:
    """Compute the transitive descendant count for each node.

    Nodes with larger subtrees should be declared first so dagre allocates
    space for their dependency chains.

    :param edges: List of (from_name, to_name) edge tuples.
    :return: Dict mapping node name to number of reachable descendants.
    """
    # Build adjacency list.
    children: dict[str, list[str]] = {}
    all_nodes: set[str] = set()
    for from_name, to_name in edges:
        children.setdefault(from_name, []).append(to_name)
        all_nodes.add(from_name)
        all_nodes.add(to_name)

    def _dfs(node: str, visited: set[str]) -> set[str]:
        """Return the set of all reachable descendants of `node`."""
        if node in visited:
            return set()
        visited.add(node)
        reachable: set[str] = set()
        for child in children.get(node, []):
            reachable.add(child)
            reachable.update(_dfs(child, visited))
        return reachable

    # Each node gets its own traversal: a dependency graph is a DAG whose
    # subtrees overlap, so a descendant set cannot be reused as a partial
    # result of its parent's without merging the two, which costs as much as
    # walking it again at this graph's size.
    return {node: len(_dfs(node, set())) for node in all_nodes}


def _compute_node_depths(
    root_name: str,
    edges: list[tuple[str, str]],
) -> dict[str, int]:
    """Compute BFS depth from root for each node.

    Edges from shallower sources should be declared first to establish
    a natural left-to-right flow in the dagre layout.

    :param root_name: The root package name.
    :param edges: List of (from_name, to_name) edge tuples.
    :return: Dict mapping node name to BFS depth from root.
    """
    adjacency: dict[str, list[str]] = {}
    for from_name, to_name in edges:
        adjacency.setdefault(from_name, []).append(to_name)

    depths: dict[str, int] = {root_name: 0}
    frontier = [root_name]
    while frontier:
        next_frontier: list[str] = []
        for node in frontier:
            for neighbor in adjacency.get(node, []):
                if neighbor not in depths:
                    depths[neighbor] = depths[node] + 1
                    next_frontier.append(neighbor)
        frontier = next_frontier
    return depths


def render_mermaid(
    root_name: str,
    packages: set[str],
    edges: list[tuple[str, str]],
    subgraphs: list[Subgraph] | None = None,
    lock_specs: LockSpecifiers | None = None,
) -> str:
    """Render the dependency graph as a Mermaid flowchart.

    ```{warning}
    Output must stay compatible with the Mermaid version bundled in
    `sphinxcontrib-mermaid`. See module docstring for details.
    ```

    Every box holds only directly-declared dependencies, drawn as hexagons
    with a thick border; transitive dependencies render outside the boxes as
    plain ovals. See the module docstring.

    :param root_name: The root package name (used to highlight it).
    :param packages: Package names to render as nodes.
    :param edges: List of (from_name, to_name) edge tuples.
    :param subgraphs: Boxes to render, in display order (extras before groups
        keeps them closer to the main dependencies). See {class}`Subgraph`.
    :param lock_specs: Optional specifiers extracted from `uv.lock`. Provides
        edge labels (`by_package`) and subgraph node labels (`by_subgraph`).
    :return: Mermaid flowchart string.
    """
    lines = ["flowchart LR"]
    subgraphs = subgraphs or []

    # Complete the node set from edges in case some endpoints are missing.
    packages = set(packages)
    for from_name, to_name in edges:
        packages.add(from_name)
        packages.add(to_name)

    # Map each box-owned package to its subgraph ID.
    package_to_subgraph: dict[str, str] = {
        pkg: subgraph.mermaid_id for subgraph in subgraphs for pkg in subgraph.owned
    }

    # Identify primary dependencies (explicitly declared in pyproject.toml) from root.
    primary_deps: set[str] = {
        to_name
        for from_name, to_name in edges
        if from_name == root_name and to_name not in package_to_subgraph
    }

    # Directly-declared dependencies across all boxes: the root's primary deps
    # plus every box's owned packages, all rendered with a thick border.
    declared_deps: set[str] = set(primary_deps)
    for subgraph in subgraphs:
        declared_deps |= subgraph.owned

    # Pre-compute graph metrics for smarter declaration ordering.
    # Dagre uses declaration order as a heuristic for node positioning,
    # so ordering by connectivity produces fewer edge crossings.
    unique_edges = list(set(edges))
    degree = _compute_node_degrees(unique_edges)
    subtree = _compute_subtree_sizes(unique_edges)
    depth = _compute_node_depths(root_name, unique_edges)

    # Transitive dependencies live outside every box.
    transitive_packages = (
        packages - set(package_to_subgraph) - primary_deps - {root_name}
    )

    # Define root node first.
    if root_name in packages:
        root_id = normalize_package_name(root_name)
        lines.append(f'    {root_id}[["`{root_name}`"]]')

    # Define primary dependencies in a subgraph to align them vertically.
    # Primary deps use hexagon shape to distinguish them from transitive deps.
    if primary_deps:
        lines.append("")
        lines.append("    subgraph primary-deps [Primary dependencies]")
        for name in sorted(
            primary_deps,
            key=lambda n: (-subtree.get(n, 0), -degree.get(n, 0), n),
        ):
            node_id = normalize_package_name(name)
            lines.append(f'        {node_id}{{{{"`{name}`"}}}}')
        lines.append("    end")

    # Define transitive dependency nodes.
    if transitive_packages:
        lines.append("")
        for name in sorted(
            transitive_packages,
            key=lambda n: (-degree.get(n, 0), n),
        ):
            node_id = normalize_package_name(name)
            lines.append(f'    {node_id}(["`{name}`"])')

    # Render the group/extra boxes. Duplicate headline nodes (packages the box
    # declares directly but a sibling box owns as a real node) get a
    # box-prefixed node ID so Mermaid keeps them in this box instead of merging
    # them into the owner's; they reuse the real node's label and link. A box
    # whose packages were all filtered out (depth trim, package focus) is
    # dropped entirely, along with its dashed arrow and style line.
    duplicate_nodes: list[tuple[str, str]] = []
    rendered_subgraphs: list[Subgraph] = []
    for subgraph in subgraphs:
        owned_names = subgraph.owned & packages
        dup_names = subgraph.duplicates & packages
        if not owned_names and not dup_names:
            continue
        sg_specs = lock_specs.by_subgraph.get(subgraph.name, {}) if lock_specs else {}
        lines.append("")
        lines.append(f"    subgraph {subgraph.mermaid_id} [{subgraph.title}]")
        for pkg in sorted(owned_names, key=lambda n: (-degree.get(n, 0), n)):
            node_id = normalize_package_name(pkg)
            spec = sg_specs.get(pkg, "")
            label = f"{pkg} {spec}" if spec else pkg
            lines.append(f'        {node_id}{{{{"`{label}`"}}}}')
        for pkg in sorted(dup_names, key=lambda n: (-degree.get(n, 0), n)):
            node_id = f"{subgraph.mermaid_id}_{normalize_package_name(pkg)}"
            spec = sg_specs.get(pkg, "")
            label = f"{pkg} {spec}" if spec else pkg
            lines.append(f'        {node_id}{{{{"`{label}`"}}}}')
            duplicate_nodes.append((node_id, pkg))
        lines.append("    end")
        rendered_subgraphs.append(subgraph)

    # Add edges. Use thick arrows for edges leaving the root (direct deps).
    # Use dashed arrows from root to subgraphs for group/extra dependencies.
    lines.append("")

    for from_name, to_name in sorted(
        set(edges),
        key=lambda e: (
            depth.get(e[0], 0),
            -degree.get(e[0], 0),
            e[0],
            -degree.get(e[1], 0),
            e[1],
        ),
    ):
        # A root edge into a box is replaced by the box's dashed arrow below.
        if from_name == root_name and to_name in package_to_subgraph:
            continue

        from_id = normalize_package_name(from_name)
        to_id = normalize_package_name(to_name)
        # Thick arrows mark direct dependencies of the root only. A primary dep
        # is already distinguished as a node (hexagon + thick border), so a
        # transitive edge that merely lands on one stays thin: bolding it too
        # made chains like `root ==> extra-platforms ==> pytest` read as a
        # single "primary" path, though pytest is only reachable via an extra.
        arrow = "==>" if from_name == root_name else "-->"

        # Add specifier as edge label if available.
        spec = ""
        if lock_specs and from_name in lock_specs.by_package:
            spec = lock_specs.by_package[from_name].get(to_name, "")
        if spec:
            lines.append(f'    {from_id} {arrow}|" {spec} "| {to_id}')
        else:
            lines.append(f"    {from_id} {arrow} {to_id}")

    # Add a dashed arrow from root to each rendered box: boxes always hang off
    # the root, since they only hold directly-declared dependencies.
    root_id = normalize_package_name(root_name)
    lines.extend(
        f"    {root_id} -.-> {subgraph_id}"
        for subgraph_id in sorted(
            subgraph.mermaid_id for subgraph in rendered_subgraphs
        )
    )

    # Tie each duplicate headline to the real node it mirrors, so both boxes
    # read as installing the same package. Dotted and arrowless, to not be
    # confused with a dependency edge.
    lines.extend(
        f"    {node_id} -.- {normalize_package_name(name)}"
        for node_id, name in duplicate_nodes
    )

    # Add click links to PyPI for each package.
    lines.append("")
    for name in sorted(packages):
        node_id = normalize_package_name(name)
        pypi_url = PYPI_PACKAGE_URL.format(package=name)
        lines.append(f'    click {node_id} "{pypi_url}" _blank')
    # Duplicate headline nodes link to the same PyPI page as their real node.
    for node_id, name in duplicate_nodes:
        pypi_url = PYPI_PACKAGE_URL.format(package=name)
        lines.append(f'    click {node_id} "{pypi_url}" _blank')

    # Style root and directly-declared dependency nodes with thick borders.
    lines.append("")
    if root_name in packages:
        root_id = normalize_package_name(root_name)
        lines.append(f"    style {root_id} {STYLE_PRIMARY_NODE}")
    for name in sorted(declared_deps):
        if name in packages:
            node_id = normalize_package_name(name)
            lines.append(f"    style {node_id} {STYLE_PRIMARY_NODE}")
    # Duplicate headline nodes keep the thick primary border, dashed to mark
    # them as mirrors of their real node.
    for node_id, _name in duplicate_nodes:
        lines.append(f"    style {node_id} {STYLE_DUPLICATE_NODE}")

    # Style boxes with per-kind colors, keyed on rendered boxes so a skipped
    # box never leaves a dangling style line.
    lines.append("")
    if primary_deps:
        lines.append(f"    style primary-deps {STYLE_PRIMARY_DEPS_SUBGRAPH}")
    lines.extend(
        f"    style {subgraph.mermaid_id} {subgraph.kind.style}"
        for subgraph in rendered_subgraphs
    )

    return "\n".join(lines)


def attribute_subgraph_packages(
    subgraph_closures: list[tuple[str, set[str]]],
    base_packages: set[str],
    direct_packages: dict[str, set[str]],
    edges: list[tuple[str, str]],
    root_name: str,
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Attribute each directly-declared package to one owning subgraph box.

    Boxes only hold the packages their group/extra declares directly;
    transitive dependencies stay outside every box (see the module docstring).
    A directly-declared package can still be claimed by several boxes, but a
    graph node can live in only one: the declarer whose closure holds the most
    dependents wins the real node (declaration order breaks ties), since
    arrows point where the package is consumed and the busiest box is its most
    natural home. The root is not a dependent, as it reaches every declared
    package by definition.

    The losing declarers list the package as a *duplicate headline* so every
    box still shows the dependency it exists to install (rendered as a
    display-only duplicate node by {func}`render_mermaid`). For example the
    `carapace` and `yaml` extras both declare only `pyyaml`, which no other
    package depends on: the dependent counts tie at zero, `carapace` owns the
    node by declaration order, and `yaml` carries `pyyaml` as a duplicate.

    :param subgraph_closures: Ordered `(name, closure_package_names)` pairs.
        Order is the last-resort tie-break for shared packages (first wins).
    :param base_packages: Packages in the base set, excluded from every box.
    :param direct_packages: Map of subgraph name to the package names it declares
        directly (from `uv.lock`), keyed by SBOM-normalized name.
    :param edges: `(from_name, to_name)` dependency edges from the full SBOM,
        used to count each declaring subgraph's local dependents.
    :param root_name: The root package name, excluded from dependent counts.
    :return: `(owned, duplicates)`. *owned* maps each subgraph to the declared
        packages it renders as real nodes; *duplicates* maps it to declared
        packages owned by a sibling box.
    """
    owned: dict[str, set[str]] = {name: set() for name, _ in subgraph_closures}
    duplicates: dict[str, set[str]] = {name: set() for name, _ in subgraph_closures}

    # Non-root packages depending on each package, for the ownership contest.
    dependents: dict[str, set[str]] = {}
    for from_name, to_name in edges:
        if from_name != root_name:
            dependents.setdefault(to_name, set()).add(from_name)

    closures = dict(subgraph_closures)
    rank = {name: index for index, (name, _) in enumerate(subgraph_closures)}

    # Reserve each declared package for the winning declarer: the one whose
    # closure holds the most dependents; earliest declaration on a tie.
    declarers: dict[str, list[str]] = {}
    for name, closure in subgraph_closures:
        for pkg in (direct_packages.get(name, set()) & closure) - base_packages:
            declarers.setdefault(pkg, []).append(name)
    for pkg, declaring in declarers.items():
        pkg_dependents = dependents.get(pkg, set())
        *_, winner = max(
            (len(pkg_dependents & closures[name]), -rank[name], name)
            for name in declaring
        )
        owned[winner].add(pkg)

    # A declaration owned by another declarer becomes a duplicate.
    for name, closure in subgraph_closures:
        direct = (direct_packages.get(name, set()) & closure) - base_packages
        duplicates[name] = direct - owned[name]

    return owned, duplicates


def generate_dependency_graph(
    package: str | None = None,
    groups: tuple[str, ...] | None = None,
    extras: tuple[str, ...] | None = None,
    frozen: bool = True,
    depth: int | None = None,
    exclude_base: bool = False,
) -> str:
    """Generate a Mermaid dependency graph.

    Each requested group/extra renders as a box holding only the packages it
    declares directly; the transitive dependencies they pull in render outside
    the boxes, like the transitive dependencies of the main set.

    :param package: Optional package name to focus on. If None, shows the entire
        project dependency tree.
    :param groups: Optional dependency groups to include (e.g., "test", "typing").
    :param extras: Optional extras to include (e.g., "xml", "json5").
    :param frozen: If True, use --frozen to skip lock file updates.
    :param depth: Optional maximum depth from root. If None, shows the full tree.
    :param exclude_base: If True, exclude main (base) dependencies from the graph,
        showing only packages unique to the requested groups/extras. Used by
        `--only-group` and `--only-extra`.
    :return: The graph in Mermaid format.
    """
    # Get the full SBOM with all requested groups and extras.
    sbom = get_cyclonedx_sbom(groups=groups, extras=extras, frozen=frozen)
    root_name, packages, edges = build_dependency_graph(sbom)

    # Parse specifiers from uv.lock for edge labels and subgraph node labels.
    lock_specs = parse_lock_specifiers(lock_data=load_lock_data())

    # Get base packages (without any groups or extras) for comparison.
    base_sbom = get_cyclonedx_sbom(frozen=frozen)
    base_packages = get_package_names_from_sbom(base_sbom)

    # Closure and directly-declared packages for every requested subgraph.
    # Groups precede extras so a package both declare lands in the group's box
    # on a dependent-count tie.
    # by_subgraph keys are SBOM-normalized names, matching render_mermaid labels.
    subgraph_closures: list[tuple[str, set[str]]] = []
    direct_packages: dict[str, set[str]] = {}
    for group in groups or ():
        closure = get_package_names_from_sbom(
            get_cyclonedx_sbom(groups=(group,), frozen=frozen)
        )
        subgraph_closures.append((group, closure))
        direct_packages[group] = set(lock_specs.by_subgraph.get(group, {}))
    for extra in extras or ():
        closure = get_package_names_from_sbom(
            get_cyclonedx_sbom(extras=(extra,), frozen=frozen)
        )
        subgraph_closures.append((extra, closure))
        direct_packages[extra] = set(lock_specs.by_subgraph.get(extra, {}))

    owned, duplicates = attribute_subgraph_packages(
        subgraph_closures, base_packages, direct_packages, edges, root_name
    )

    # Boxes in display order: extras before groups, closer to the main deps.
    subgraphs = [
        Subgraph(SubgraphKind.EXTRA, extra, owned[extra], duplicates[extra])
        for extra in sorted(extras or ())
    ]
    subgraphs += [
        Subgraph(SubgraphKind.GROUP, group, owned[group], duplicates[group])
        for group in sorted(groups or ())
    ]

    # Discard the root edges uv's export invents for dependency-group packages
    # an extra happens to pull in. See {func}`filter_root_edges`.
    root_main_deps = lock_specs.by_main.get(root_name)
    edges = filter_root_edges(
        root_name,
        edges,
        None if root_main_deps is None else set(root_main_deps),
        subgraphs,
    )

    # CycloneDX SBOMs carry no edge from root to the packages its extras
    # activate (they only appear as dependencies of the package whose extra
    # pulls them in, e.g. click-extra -> xmltodict). Synthetic root edges to
    # each extra's declared packages keep them reachable under --package
    # filtering and pin them at depth 1, like group headliners; their
    # transitive dependencies sit at depth 2+ through regular SBOM edges.
    # Groups need no synthetic edges: the SBOM already links root to their
    # declared packages.
    for subgraph in subgraphs:
        if subgraph.kind is SubgraphKind.EXTRA:
            edges.extend((root_name, pkg) for pkg in subgraph.owned)

    # Exclude base (main) dependencies when --only-group/--only-extra is used.
    # Keeps the root node and every package the requested groups/extras pull
    # in (declared and transitive), removing the base dependency set.
    if exclude_base:
        subgraph_union: set[str] = set()
        for _name, closure in subgraph_closures:
            subgraph_union |= closure
        allowed = (subgraph_union - base_packages) | {root_name}
        packages &= allowed
        edges = [
            (from_name, to_name)
            for from_name, to_name in edges
            if from_name in allowed and to_name in allowed
        ]

    # Filter to specific package if requested.
    if package:
        packages, edges = filter_graph_to_package(packages, edges, package)
        root_name = package

    # Trim graph to maximum depth if requested.
    if depth is not None:
        packages, edges = trim_graph_to_depth(root_name, packages, edges, depth)

    return render_mermaid(root_name, packages, edges, subgraphs, lock_specs)
