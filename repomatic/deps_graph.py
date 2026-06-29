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
from functools import lru_cache
from pathlib import Path

import tomlrt

from repomatic.uv import load_lock_data, parse_lock_specifiers, uv_cmd

TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Any

    from repomatic.uv import LockSpecifiers


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
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
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


def parse_bom_ref(bom_ref: str) -> tuple[str, str]:
    """Parse a CycloneDX bom-ref into package name and version.

    The format is typically `name-index@version` (e.g., `click-extra-11@7.4.0`).

    :param bom_ref: The bom-ref string from CycloneDX.
    :return: Tuple of (package_name, version).
    """
    # Split on @ to get version.
    if "@" in bom_ref:
        name_part, version = bom_ref.rsplit("@", 1)
    else:
        name_part = bom_ref
        version = ""

    # Remove trailing index (e.g., "click-extra-11" -> "click-extra").
    # The index is a number added by uv to ensure uniqueness.
    match = re.match(r"^(.+)-(\d+)$", name_part)
    if match:
        name = match.group(1)
    else:
        name = name_part

    return name, version


def get_available_groups(pyproject_path: Path | None = None) -> tuple[str, ...]:
    """Discover available dependency groups from pyproject.toml.

    :param pyproject_path: Path to pyproject.toml. If None, looks in current directory.
    :return: Tuple of group names.
    """
    if pyproject_path is None:
        pyproject_path = Path("pyproject.toml")

    if not pyproject_path.exists():
        return ()

    with pyproject_path.open("rb") as f:
        pyproject = tomlrt.load(f)

    groups = pyproject.get("dependency-groups", {})
    return tuple(sorted(groups.keys()))


def get_available_extras(pyproject_path: Path | None = None) -> tuple[str, ...]:
    """Discover available optional extras from pyproject.toml.

    :param pyproject_path: Path to pyproject.toml. If None, looks in current directory.
    :return: Tuple of extra names.
    """
    if pyproject_path is None:
        pyproject_path = Path("pyproject.toml")

    if not pyproject_path.exists():
        return ()

    with pyproject_path.open("rb") as f:
        pyproject = tomlrt.load(f)

    project = pyproject.get("project", {})
    extras = project.get("optional-dependencies", {})
    return tuple(sorted(extras.keys()))


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
    root_package: str | None = None,
) -> tuple[str, dict[str, tuple[str, str]], list[tuple[str, str]]]:
    """Build a dependency graph from CycloneDX SBOM data.

    :param sbom: Parsed CycloneDX SBOM dictionary.
    :param root_package: Optional package name to use as root. If None, uses the
        metadata component from the SBOM.
    :return: Tuple of (root_name, nodes_dict, edges_list) where:
        - root_name is the root package name
        - nodes_dict maps bom-ref to (name, version) tuples
        - edges_list is a list of (from_name, to_name) tuples
    """
    # Build a mapping from bom-ref to (name, version).
    nodes: dict[str, tuple[str, str]] = {}

    # Get root package info from metadata.
    metadata = sbom.get("metadata", {})
    root_component = metadata.get("component", {})
    root_ref = root_component.get("bom-ref", "")
    root_name = root_component.get("name", "")
    root_version = root_component.get("version", "")

    if root_ref:
        nodes[root_ref] = (root_name, root_version)

    # Add all components.
    for component in sbom.get("components", []):
        bom_ref = component.get("bom-ref", "")
        name = component.get("name", "")
        version = component.get("version", "")
        if bom_ref and name:
            nodes[bom_ref] = (name, version)

    # Build edges from dependencies.
    edges: list[tuple[str, str]] = []
    for dep in sbom.get("dependencies", []):
        from_ref = dep.get("ref", "")
        depends_on = dep.get("dependsOn", [])

        if from_ref not in nodes:
            continue

        from_name, _ = nodes[from_ref]

        for to_ref in depends_on:
            if to_ref in nodes:
                to_name, _ = nodes[to_ref]
                edges.append((from_name, to_name))

    # Filter to root package if specified.
    if root_package:
        root_name = root_package

    return root_name, nodes, edges


def filter_graph_to_package(
    root_name: str,
    nodes: dict[str, tuple[str, str]],
    edges: list[tuple[str, str]],
    package: str,
) -> tuple[dict[str, tuple[str, str]], list[tuple[str, str]]]:
    """Filter the graph to only include dependencies of a specific package.

    :param root_name: The root package name.
    :param nodes: Dictionary mapping bom-ref to (name, version) tuples.
    :param edges: List of (from_name, to_name) edge tuples.
    :param package: Package name to filter to.
    :return: Filtered (nodes, edges) tuple.
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

    # Filter nodes to only those that appear in edges or are the target.
    used_names = {package}
    for from_name, to_name in filtered_edges:
        used_names.add(from_name)
        used_names.add(to_name)

    filtered_nodes = {
        ref: (name, version)
        for ref, (name, version) in nodes.items()
        if name in used_names
    }

    return filtered_nodes, filtered_edges


def trim_graph_to_depth(
    root_name: str,
    nodes: dict[str, tuple[str, str]],
    edges: list[tuple[str, str]],
    depth: int,
) -> tuple[dict[str, tuple[str, str]], list[tuple[str, str]]]:
    """Trim the graph to only include nodes within a given depth from the root.

    Performs a breadth-first traversal from the root, keeping only nodes
    reachable within `depth` hops and edges between those nodes.

    :param root_name: The root package name.
    :param nodes: Dictionary mapping bom-ref to (name, version) tuples.
    :param edges: List of (from_name, to_name) edge tuples.
    :param depth: Maximum depth from root. 0 = root only, 1 = root + primary deps, etc.
    :return: Filtered (nodes, edges) tuple.
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

    # Filter nodes to only reachable ones.
    filtered_nodes = {
        ref: (name, version)
        for ref, (name, version) in nodes.items()
        if name in reachable
    }

    return filtered_nodes, filtered_edges


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

    cache: dict[str, int] = {}

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

    for node in all_nodes:
        if node not in cache:
            cache[node] = len(_dfs(node, set()))
    return cache


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
    nodes: dict[str, tuple[str, str]],
    edges: list[tuple[str, str]],
    group_packages: dict[str, set[str]] | None = None,
    extra_packages: dict[str, set[str]] | None = None,
    lock_specs: LockSpecifiers | None = None,
    group_duplicates: dict[str, set[str]] | None = None,
    extra_duplicates: dict[str, set[str]] | None = None,
) -> str:
    """Render the dependency graph as a Mermaid flowchart.

    ```{warning}
    Output must stay compatible with the Mermaid version bundled in
    `sphinxcontrib-mermaid`. See module docstring for details.
    ```

    :param root_name: The root package name (used to highlight it).
    :param nodes: Dictionary mapping bom-ref to (name, version) tuples.
    :param edges: List of (from_name, to_name) edge tuples.
    :param group_packages: Optional dict mapping group names to sets of package names
        that are unique to that group. These will be rendered in subgraphs with
        `--group` prefix.
    :param extra_packages: Optional dict mapping extra names to sets of package names
        that are unique to that extra. These will be rendered in subgraphs with
        `--extra` prefix.
    :param lock_specs: Optional specifiers extracted from `uv.lock`. Provides
        edge labels (`by_package`) and subgraph node labels (`by_subgraph`).
    :param group_duplicates: Optional dict mapping group names to package names the
        group declares directly but that another subgraph owns as a real node.
        Rendered as display-only duplicate nodes so every group still shows its
        headline dependency. See {func}`attribute_subgraph_packages`.
    :param extra_duplicates: Optional dict mapping extra names to package names the
        extra declares directly but that another subgraph owns as a real node.
        Rendered as display-only duplicate nodes so every extra still shows its
        headline dependency. See {func}`attribute_subgraph_packages`.
    :return: Mermaid flowchart string.
    """
    lines = ["flowchart LR"]

    # Collect all unique package names.
    packages: set[str] = set()
    for name, _version in nodes.values():
        packages.add(name)

    # Also collect from edges in case some are missing from nodes.
    for from_name, to_name in edges:
        packages.add(from_name)
        packages.add(to_name)

    # Determine which packages belong to which group or extra.
    # Subgraph IDs are prefixed with `grp_` / `ext_` to avoid collisions
    # with node IDs (e.g., the `json5` package inside a `json5` extra).
    package_to_subgraph: dict[str, str] = {}
    if group_packages:
        for group_name, pkg_names in group_packages.items():
            for pkg_name in pkg_names:
                package_to_subgraph[pkg_name] = f"grp_{group_name}"
    if extra_packages:
        for extra_name, pkg_names in extra_packages.items():
            for pkg_name in pkg_names:
                package_to_subgraph[pkg_name] = f"ext_{extra_name}"

    # Identify primary dependencies (explicitly declared in pyproject.toml) from root.
    primary_deps: set[str] = set()
    for from_name, to_name in edges:
        if from_name == root_name and to_name not in package_to_subgraph:
            primary_deps.add(to_name)

    # Build the full set of primary deps across all subgraphs.
    all_primary_deps: set[str] = set(primary_deps)
    if lock_specs:
        for sg_deps in lock_specs.by_subgraph.values():
            all_primary_deps.update(sg_deps.keys())

    # Pre-compute graph metrics for smarter declaration ordering.
    # Dagre uses declaration order as a heuristic for node positioning,
    # so ordering by connectivity produces fewer edge crossings.
    unique_edges = list(set(edges))
    degree = _compute_node_degrees(unique_edges)
    subtree = _compute_subtree_sizes(unique_edges)
    depth = _compute_node_depths(root_name, unique_edges)

    # Separate packages into: root, primary deps, other, and subgraph-specific.
    other_main_packages = {
        name
        for name in packages
        if name not in package_to_subgraph
        and name not in primary_deps
        and name != root_name
    }

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

    # Define other main nodes (transitive dependencies).
    if other_main_packages:
        lines.append("")
        for name in sorted(
            other_main_packages,
            key=lambda n: (-degree.get(n, 0), n),
        ):
            node_id = normalize_package_name(name)
            lines.append(f'    {node_id}(["`{name}`"])')

    # Render one extra/group subgraph. Duplicate headline nodes (packages the
    # subgraph declares directly but another subgraph owns as a real node) get a
    # subgraph-prefixed node ID so Mermaid keeps them in this box instead of
    # merging them into the owner's; they reuse the real node's label and link.
    duplicate_nodes: list[tuple[str, str]] = []
    emitted_subgraph_ids: set[str] = set()

    def _emit_subgraph(
        sg_id: str,
        header: str,
        name: str,
        primary_names: set[str],
        dup_names: set[str],
    ) -> None:
        if not primary_names and not dup_names:
            return
        sg_specs = lock_specs.by_subgraph.get(name, {}) if lock_specs else {}
        lines.append("")
        lines.append(f"    subgraph {sg_id} [{header}]")
        for pkg in sorted(primary_names, key=lambda n: (-degree.get(n, 0), n)):
            if pkg not in packages:
                continue
            node_id = normalize_package_name(pkg)
            spec = sg_specs.get(pkg, "")
            label = f"{pkg} {spec}" if spec else pkg
            # Primary deps use hexagon shape.
            if pkg in all_primary_deps:
                lines.append(f'        {node_id}{{{{"`{label}`"}}}}')
            else:
                lines.append(f'        {node_id}(["`{label}`"])')
        for pkg in sorted(dup_names, key=lambda n: (-degree.get(n, 0), n)):
            if pkg not in packages:
                continue
            # Headline deps are always primary, so duplicates use hexagon shape.
            node_id = f"{sg_id}_{normalize_package_name(pkg)}"
            spec = sg_specs.get(pkg, "")
            label = f"{pkg} {spec}" if spec else pkg
            lines.append(f'        {node_id}{{{{"`{label}`"}}}}')
            duplicate_nodes.append((node_id, pkg))
        lines.append("    end")
        emitted_subgraph_ids.add(sg_id)

    # Define extra subgraphs (before groups so they appear closer to main deps).
    if extra_packages:
        for extra_name in sorted(extra_packages.keys()):
            dup = extra_duplicates.get(extra_name, set()) if extra_duplicates else set()
            _emit_subgraph(
                f"ext_{extra_name}",
                f"--extra {extra_name}",
                extra_name,
                extra_packages[extra_name],
                dup,
            )

    # Define group subgraphs (after extras, further from main deps).
    if group_packages:
        for group_name in sorted(group_packages.keys()):
            dup = group_duplicates.get(group_name, set()) if group_duplicates else set()
            _emit_subgraph(
                f"grp_{group_name}",
                f"--group {group_name}",
                group_name,
                group_packages[group_name],
                dup,
            )

    # Add edges. Use thick arrows for edges leaving the root (direct deps).
    # Use dashed arrows from root to subgraphs for group/extra dependencies.
    lines.append("")

    # Track which subgraphs have edges from root.
    root_to_subgraphs: set[str] = set()

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
        # Check if target is in a subgraph and source is root.
        if from_name == root_name and to_name in package_to_subgraph:
            # Track that we need a link to this subgraph.
            root_to_subgraphs.add(package_to_subgraph[to_name])
            continue  # Skip individual edge, will link to subgraph instead.

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

    # Add dashed arrows from root to each rendered subgraph. A duplicate-only
    # box (no primary node, so no root edge feeds root_to_subgraphs) still needs
    # its arrow, so union in every emitted subgraph.
    root_id = normalize_package_name(root_name)
    lines.extend(
        f"    {root_id} -.-> {subgraph_name}"
        for subgraph_name in sorted(root_to_subgraphs | emitted_subgraph_ids)
    )

    # Add click links to PyPI for each package.
    lines.append("")
    for name in sorted(packages):
        node_id = normalize_package_name(name)
        pypi_url = f"https://pypi.org/project/{name}/"
        lines.append(f'    click {node_id} "{pypi_url}" _blank')
    # Duplicate headline nodes link to the same PyPI page as their real node.
    for node_id, name in duplicate_nodes:
        pypi_url = f"https://pypi.org/project/{name}/"
        lines.append(f'    click {node_id} "{pypi_url}" _blank')

    # Style root and primary dependency nodes with thick borders.
    lines.append("")
    if root_name in packages:
        root_id = normalize_package_name(root_name)
        lines.append(f"    style {root_id} {STYLE_PRIMARY_NODE}")
    if all_primary_deps:
        for name in sorted(all_primary_deps):
            if name in packages:
                node_id = normalize_package_name(name)
                lines.append(f"    style {node_id} {STYLE_PRIMARY_NODE}")
    # Duplicate headline nodes get the same thick border as their real node.
    for node_id, _name in duplicate_nodes:
        lines.append(f"    style {node_id} {STYLE_PRIMARY_NODE}")

    # Style subgraphs with different colors. Key on emitted boxes, not just
    # those with a primary node, so a duplicate-only box still gets its tint.
    lines.append("")
    if primary_deps:
        lines.append(f"    style primary-deps {STYLE_PRIMARY_DEPS_SUBGRAPH}")
    if extra_packages:
        lines.extend(
            f"    style ext_{extra_name} {STYLE_EXTRA_SUBGRAPH}"
            for extra_name in sorted(extra_packages.keys())
            if f"ext_{extra_name}" in emitted_subgraph_ids
        )
    if group_packages:
        lines.extend(
            f"    style grp_{group_name} {STYLE_GROUP_SUBGRAPH}"
            for group_name in sorted(group_packages.keys())
            if f"grp_{group_name}" in emitted_subgraph_ids
        )

    return "\n".join(lines)


def attribute_subgraph_packages(
    subgraph_closures: list[tuple[str, set[str]]],
    base_packages: set[str],
    direct_packages: dict[str, set[str]],
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Attribute each package to one owning subgraph, plus duplicate headlines.

    A dependency graph node can live in only one subgraph box, but several
    extras/groups can pull the same package. Two passes decide ownership so a
    subgraph's directly-declared (headline) dependency is never stolen by a
    sibling that only reaches it transitively:

    1. Reserve each subgraph's direct declarations (first declarer wins on a
       direct-vs-direct tie).
    2. Attribute the remaining transitive packages first-come.

    When several subgraphs declare the same package directly, the first owns the
    real node; the others list it as a *duplicate headline* so every extra/group
    still shows the dependency it exists to install (rendered as a display-only
    duplicate node by {func}`render_mermaid`). For example the `carapace` and
    `yaml` extras both declare only `pyyaml`: `carapace` owns the node and `yaml`
    carries `pyyaml` as a duplicate.

    :param subgraph_closures: Ordered ``(name, closure_package_names)`` pairs.
        Order is the tie-break for shared packages (first wins).
    :param base_packages: Packages in the base set, excluded from every subgraph.
    :param direct_packages: Map of subgraph name to the package names it declares
        directly (from `uv.lock`), keyed by SBOM-normalized name.
    :return: ``(primary, duplicates)``. *primary* maps each subgraph to the
        packages it owns as real nodes; *duplicates* maps it to directly-declared
        packages owned by another subgraph.
    """
    primary: dict[str, set[str]] = {name: set() for name, _ in subgraph_closures}
    duplicates: dict[str, set[str]] = {name: set() for name, _ in subgraph_closures}
    seen: set[str] = set()

    # Pass 1: reserve directly-declared headline packages (first declarer wins).
    for name, closure in subgraph_closures:
        direct = (direct_packages.get(name, set()) & closure) - base_packages
        owned = direct - seen
        primary[name] |= owned
        seen |= owned

    # Pass 2: attribute the remaining transitive packages first-come.
    for name, closure in subgraph_closures:
        transitive = closure - base_packages - seen
        primary[name] |= transitive
        seen |= transitive

    # A direct declaration owned by an earlier sibling becomes a duplicate.
    for name, closure in subgraph_closures:
        direct = (direct_packages.get(name, set()) & closure) - base_packages
        duplicates[name] = direct - primary[name]

    return primary, duplicates


def generate_dependency_graph(
    package: str | None = None,
    groups: tuple[str, ...] | None = None,
    extras: tuple[str, ...] | None = None,
    frozen: bool = True,
    depth: int | None = None,
    exclude_base: bool = False,
) -> str:
    """Generate a Mermaid dependency graph.

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
    root_name, nodes, edges = build_dependency_graph(sbom)

    # Parse specifiers from uv.lock for edge labels and subgraph node labels.
    lock_specs = parse_lock_specifiers(lock_data=load_lock_data())

    # Get base packages (without any groups or extras) for comparison.
    base_sbom = get_cyclonedx_sbom(frozen=frozen)
    base_packages = get_package_names_from_sbom(base_sbom)

    # Closure and directly-declared packages for every requested subgraph.
    # Groups precede extras so a package only a group declares lands in its box.
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

    primary, duplicates = attribute_subgraph_packages(
        subgraph_closures, base_packages, direct_packages
    )
    group_packages = {g: primary[g] for g in groups} if groups else None
    extra_packages = {e: primary[e] for e in extras} if extras else None
    group_duplicates = {g: duplicates[g] for g in groups} if groups else None
    extra_duplicates = {e: duplicates[e] for e in extras} if extras else None

    # Every package owned by a subgraph as a real node, for exclude_base below.
    seen_in_subgraphs: set[str] = set()
    for owned in primary.values():
        seen_in_subgraphs |= owned

    # Add synthetic edges from root to extra-owned packages.
    # CycloneDX SBOMs don't include direct edges from root to packages
    # activated by extras (they appear as transitive deps of the parent
    # package, e.g. click-extra -> xmltodict). Adding synthetic edges
    # ensures extras are treated as depth-1 dependencies and get dashed
    # arrows from root to their subgraphs.
    if extra_packages:
        for extra_pkgs in extra_packages.values():
            for pkg in extra_pkgs:
                edges.append((root_name, pkg))

    # Exclude base (main) dependencies when --only-group/--only-extra is used.
    # Keeps the root node and packages unique to groups/extras, removing
    # everything that belongs to the base dependency set.
    if exclude_base:
        allowed = seen_in_subgraphs | {root_name}
        nodes = {
            ref: (name, version)
            for ref, (name, version) in nodes.items()
            if name in allowed
        }
        edges = [
            (from_name, to_name)
            for from_name, to_name in edges
            if from_name in allowed and to_name in allowed
        ]

    # Filter to specific package if requested.
    if package:
        nodes, edges = filter_graph_to_package(root_name, nodes, edges, package)
        root_name = package

    # Trim graph to maximum depth if requested.
    if depth is not None:
        nodes, edges = trim_graph_to_depth(root_name, nodes, edges, depth)

    return render_mermaid(
        root_name,
        nodes,
        edges,
        group_packages,
        extra_packages,
        lock_specs,
        group_duplicates,
        extra_duplicates,
    )
