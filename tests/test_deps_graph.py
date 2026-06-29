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

from pathlib import Path

import pytest

from repomatic.deps_graph import (
    _compute_node_degrees,
    _compute_node_depths,
    _compute_subtree_sizes,
    attribute_subgraph_packages,
    build_dependency_graph,
    filter_graph_to_package,
    get_available_extras,
    get_available_groups,
    normalize_package_name,
    parse_bom_ref,
    render_mermaid,
    trim_graph_to_depth,
)
from repomatic.uv import LockSpecifiers

# Sample CycloneDX SBOM data for testing.
SAMPLE_SBOM = {
    "metadata": {
        "component": {
            "bom-ref": "my-project-1@1.0.0",
            "name": "my-project",
            "version": "1.0.0",
        }
    },
    "components": [
        {"bom-ref": "click-2@8.0.0", "name": "click", "version": "8.0.0"},
        {"bom-ref": "requests-3@2.28.0", "name": "requests", "version": "2.28.0"},
        {"bom-ref": "urllib3-4@1.26.0", "name": "urllib3", "version": "1.26.0"},
        {"bom-ref": "certifi-5@2022.0", "name": "certifi", "version": "2022.0"},
    ],
    "dependencies": [
        {
            "ref": "my-project-1@1.0.0",
            "dependsOn": ["click-2@8.0.0", "requests-3@2.28.0"],
        },
        {"ref": "click-2@8.0.0", "dependsOn": []},
        {
            "ref": "requests-3@2.28.0",
            "dependsOn": ["urllib3-4@1.26.0", "certifi-5@2022.0"],
        },
        {"ref": "urllib3-4@1.26.0", "dependsOn": []},
        {"ref": "certifi-5@2022.0", "dependsOn": []},
    ],
}

# SBOM with dependency cycles, to assert graph traversals stay cycle-safe.
# uv's CycloneDX export can in principle contain back-edges (mutually
# dependent packages) and self-loops, so every traversal must terminate.
# Encodes a 2-cycle (apple <-> banana) and a self-loop (cherry -> cherry):
#
#     orchard -> apple -> banana -> apple   (back-edge)
#                          banana -> cherry -> cherry   (self-loop)
CYCLIC_SBOM = {
    "metadata": {
        "component": {
            "bom-ref": "orchard-1@1.0.0",
            "name": "orchard",
            "version": "1.0.0",
        }
    },
    "components": [
        {"bom-ref": "apple-2@1.0.0", "name": "apple", "version": "1.0.0"},
        {"bom-ref": "banana-3@1.0.0", "name": "banana", "version": "1.0.0"},
        {"bom-ref": "cherry-4@1.0.0", "name": "cherry", "version": "1.0.0"},
    ],
    "dependencies": [
        {"ref": "orchard-1@1.0.0", "dependsOn": ["apple-2@1.0.0"]},
        {"ref": "apple-2@1.0.0", "dependsOn": ["banana-3@1.0.0"]},
        {"ref": "banana-3@1.0.0", "dependsOn": ["apple-2@1.0.0", "cherry-4@1.0.0"]},
        {"ref": "cherry-4@1.0.0", "dependsOn": ["cherry-4@1.0.0"]},
    ],
}


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("click", "click_0"),  # Reserved Mermaid keyword.
        ("click-extra", "click_extra"),
        ("My-Package", "my_package"),
        ("package123", "package123"),
        ("foo.bar", "foo_bar"),
        ("foo_bar", "foo_bar"),
        ("graph", "graph_0"),  # Reserved Mermaid keyword.
        ("end", "end_0"),  # Reserved Mermaid keyword.
    ],
)
def test_normalize_package_name(name: str, expected: str) -> None:
    assert normalize_package_name(name) == expected


@pytest.mark.parametrize(
    ("bom_ref", "expected_name", "expected_version"),
    [
        ("click-2@8.0.0", "click", "8.0.0"),
        ("click-extra-11@7.4.0", "click-extra", "7.4.0"),
        ("my-project-1@1.0.0", "my-project", "1.0.0"),
        ("simple@1.0", "simple", "1.0"),
        ("no-version-123", "no-version", ""),
        ("urllib3-4@1.26.0", "urllib3", "1.26.0"),
    ],
)
def test_parse_bom_ref(bom_ref: str, expected_name: str, expected_version: str) -> None:
    name, version = parse_bom_ref(bom_ref)
    assert name == expected_name
    assert version == expected_version


def test_build_dependency_graph() -> None:
    root_name, nodes, edges = build_dependency_graph(SAMPLE_SBOM)

    assert root_name == "my-project"
    assert len(nodes) == 5  # root + 4 components.

    # Check nodes contain expected packages.
    node_names = {name for name, _ in nodes.values()}
    assert "my-project" in node_names
    assert "click" in node_names
    assert "requests" in node_names
    assert "urllib3" in node_names
    assert "certifi" in node_names

    # Check edges.
    assert ("my-project", "click") in edges
    assert ("my-project", "requests") in edges
    assert ("requests", "urllib3") in edges
    assert ("requests", "certifi") in edges
    assert len(edges) == 4


def test_filter_graph_to_package() -> None:
    root_name, nodes, edges = build_dependency_graph(SAMPLE_SBOM)

    # Filter to requests package.
    filtered_nodes, filtered_edges = filter_graph_to_package(
        root_name, nodes, edges, "requests"
    )

    # Should only include requests and its dependencies.
    filtered_names = {name for name, _ in filtered_nodes.values()}
    assert "requests" in filtered_names
    assert "urllib3" in filtered_names
    assert "certifi" in filtered_names
    assert "my-project" not in filtered_names
    assert "click" not in filtered_names

    # Check edges.
    assert ("requests", "urllib3") in filtered_edges
    assert ("requests", "certifi") in filtered_edges
    assert len(filtered_edges) == 2


def test_trim_graph_to_depth_zero() -> None:
    root_name, nodes, edges = build_dependency_graph(SAMPLE_SBOM)

    # Depth 0 = root only.
    trimmed_nodes, trimmed_edges = trim_graph_to_depth(root_name, nodes, edges, 0)

    trimmed_names = {name for name, _ in trimmed_nodes.values()}
    assert trimmed_names == {"my-project"}
    assert trimmed_edges == []


def test_trim_graph_to_depth_one() -> None:
    root_name, nodes, edges = build_dependency_graph(SAMPLE_SBOM)

    # Depth 1 = root + primary deps.
    trimmed_nodes, trimmed_edges = trim_graph_to_depth(root_name, nodes, edges, 1)

    trimmed_names = {name for name, _ in trimmed_nodes.values()}
    assert trimmed_names == {"my-project", "click", "requests"}
    # Only edges from root to primary deps.
    assert ("my-project", "click") in trimmed_edges
    assert ("my-project", "requests") in trimmed_edges
    assert len(trimmed_edges) == 2


def test_trim_graph_to_depth_two() -> None:
    root_name, nodes, edges = build_dependency_graph(SAMPLE_SBOM)

    # Depth 2 = root + primary deps + their deps.
    trimmed_nodes, trimmed_edges = trim_graph_to_depth(root_name, nodes, edges, 2)

    trimmed_names = {name for name, _ in trimmed_nodes.values()}
    assert trimmed_names == {"my-project", "click", "requests", "urllib3", "certifi"}
    assert len(trimmed_edges) == 4


def test_trim_graph_to_depth_exceeding() -> None:
    root_name, nodes, edges = build_dependency_graph(SAMPLE_SBOM)

    # Depth larger than the graph keeps everything.
    trimmed_nodes, trimmed_edges = trim_graph_to_depth(root_name, nodes, edges, 100)

    trimmed_names = {name for name, _ in trimmed_nodes.values()}
    assert len(trimmed_names) == 5
    assert len(trimmed_edges) == len(edges)


def test_render_mermaid() -> None:
    root_name, nodes, edges = build_dependency_graph(SAMPLE_SBOM)
    output = render_mermaid(root_name, nodes, edges)

    assert output.startswith("flowchart LR")
    # "click" is a reserved Mermaid keyword, so it gets "_0" suffix.
    # Primary deps use hexagon shape with markdown backticks.
    assert 'click_0{{"`click`"}}' in output
    # Root uses subprocess (subroutine) shape.
    assert 'my_project[["`my-project`"]]' in output
    # Versions are not included in node labels.
    assert "v8.0.0" not in output
    # Primary dependencies are in a subgraph for vertical alignment.
    assert "subgraph primary-deps [Primary dependencies]" in output
    # Primary dependencies use thick arrows.
    assert "my_project ==> click_0" in output
    assert "my_project ==> requests" in output
    # Transitive dependencies use normal arrows.
    assert "requests --> urllib3" in output
    # PyPI links are added for each package.
    assert 'click click_0 "https://pypi.org/project/click/" _blank' in output
    assert 'click requests "https://pypi.org/project/requests/" _blank' in output


def test_render_mermaid_with_specifiers() -> None:
    root_name, nodes, edges = build_dependency_graph(SAMPLE_SBOM)
    # Provide specifiers for edge labels.
    lock_specs = LockSpecifiers(
        by_package={
            "my-project": {"click": ">=8.0", "requests": ">=2.28"},
            "requests": {"urllib3": ">=1.26,<2", "certifi": ">=2022"},
        },
        by_subgraph={},
    )
    output = render_mermaid(root_name, nodes, edges, lock_specs=lock_specs)

    # Check edge labels with specifiers.
    assert 'my_project ==>|" >=8.0 "| click_0' in output
    assert 'my_project ==>|" >=2.28 "| requests' in output
    assert 'requests -->|" >=1.26,<2 "| urllib3' in output
    assert 'requests -->|" >=2022 "| certifi' in output


def test_render_mermaid_with_groups() -> None:
    root_name, nodes, edges = build_dependency_graph(SAMPLE_SBOM)
    # Add a test package to a group.
    group_packages = {"test": {"pytest"}}
    # Add pytest to nodes and edges.
    extended_nodes = dict(nodes)
    extended_nodes["pytest-6@7.0.0"] = ("pytest", "7.0.0")
    extended_edges = list(edges) + [("my-project", "pytest")]

    output = render_mermaid(root_name, extended_nodes, extended_edges, group_packages)

    # Group subgraph ID is prefixed to avoid collision with node IDs.
    assert "subgraph grp_test [--group test]" in output
    # Root uses dashed arrow to group subgraph.
    assert "my_project -.-> grp_test" in output


def test_render_mermaid_with_subgraph_specifiers() -> None:
    root_name, nodes, edges = build_dependency_graph(SAMPLE_SBOM)
    # Add test packages to a group.
    group_packages = {"test": {"pytest", "coverage"}}
    extended_nodes = dict(nodes)
    extended_nodes["pytest-6@7.0.0"] = ("pytest", "7.0.0")
    extended_nodes["coverage-7@7.0.0"] = ("coverage", "7.0.0")
    extended_edges = list(edges) + [
        ("my-project", "pytest"),
        ("my-project", "coverage"),
    ]
    # Specifiers for primary deps in the test group.
    lock_specs = LockSpecifiers(
        by_package={},
        by_subgraph={"test": {"pytest": ">=9", "coverage": ">=7.11"}},
    )

    output = render_mermaid(
        root_name,
        extended_nodes,
        extended_edges,
        group_packages,
        lock_specs=lock_specs,
    )

    # Primary group deps use hexagon shape with specifier in label.
    assert 'pytest{{"`pytest >=9`"}}' in output
    assert 'coverage{{"`coverage >=7.11`"}}' in output


def test_render_mermaid_primary_deps_in_subgraph() -> None:
    root_name, nodes, edges = build_dependency_graph(SAMPLE_SBOM)
    # Add test packages: pytest and pytest-cov (primary), iniconfig (transitive).
    group_packages = {"test": {"pytest", "pytest-cov", "iniconfig"}}
    extended_nodes = dict(nodes)
    extended_nodes["pytest-6@7.0.0"] = ("pytest", "7.0.0")
    extended_nodes["pytest-cov-8@7.0.0"] = ("pytest-cov", "7.0.0")
    extended_nodes["iniconfig-9@2.0.0"] = ("iniconfig", "2.0.0")
    extended_edges = list(edges) + [
        ("my-project", "pytest"),
        ("my-project", "pytest-cov"),
        ("my-project", "iniconfig"),
        ("pytest-cov", "pytest"),
        ("pytest", "iniconfig"),
    ]
    # pytest and pytest-cov are primary deps (in lock_specs.by_subgraph).
    lock_specs = LockSpecifiers(
        by_package={},
        by_subgraph={"test": {"pytest": ">=9", "pytest-cov": ">=7"}},
    )

    output = render_mermaid(
        root_name,
        extended_nodes,
        extended_edges,
        group_packages,
        lock_specs=lock_specs,
    )

    # Primary deps use hexagon shape.
    assert 'pytest{{"`pytest >=9`"}}' in output
    assert 'pytest_cov{{"`pytest-cov >=7`"}}' in output
    # Transitive dep uses round shape.
    assert 'iniconfig(["`iniconfig`"])' in output
    # A transitive edge between two non-root packages stays thin, even when it
    # points at a primary dep: only edges leaving the root are thick.
    assert "pytest_cov --> pytest" in output
    # Arrow pointing to a transitive dep uses normal style.
    assert "pytest --> iniconfig" in output


def test_render_mermaid_with_extras() -> None:
    root_name, nodes, edges = build_dependency_graph(SAMPLE_SBOM)
    # Add a package to an extra.
    extra_packages = {"xml": {"lxml"}}
    # Add lxml to nodes and edges.
    extended_nodes = dict(nodes)
    extended_nodes["lxml-7@4.9.0"] = ("lxml", "4.9.0")
    extended_edges = list(edges) + [("my-project", "lxml")]

    output = render_mermaid(
        root_name, extended_nodes, extended_edges, extra_packages=extra_packages
    )

    # Extra subgraph ID is prefixed to avoid collision with node IDs.
    assert "subgraph ext_xml [--extra xml]" in output
    # Root uses dashed arrow to extra subgraph.
    assert "my_project -.-> ext_xml" in output


def test_attribute_subgraph_packages_duplicate_headlines() -> None:
    # Three subgraphs share the directly-declared package `sugar`; `water` is a
    # base dependency excluded from every box.
    subgraph_closures = [
        ("bakery", {"flour", "sugar", "water"}),
        ("cafe", {"sugar", "water"}),
        ("diner", {"sugar", "plate", "water"}),
    ]
    direct_packages = {
        "bakery": {"flour", "sugar"},
        "cafe": {"sugar"},
        "diner": {"sugar", "plate"},
    }
    primary, duplicates = attribute_subgraph_packages(
        subgraph_closures, {"water"}, direct_packages
    )
    # First declarer (processing order) owns the shared headline as a real node.
    assert primary["bakery"] == {"flour", "sugar"}
    assert primary["cafe"] == set()
    assert primary["diner"] == {"plate"}
    # Base dependency never lands in a subgraph.
    assert all("water" not in pkgs for pkgs in primary.values())
    # Siblings that also declare `sugar` keep it as a duplicate headline, so
    # their box still renders the dependency they exist to install.
    assert duplicates["bakery"] == set()
    assert duplicates["cafe"] == {"sugar"}
    assert duplicates["diner"] == {"sugar"}


def test_attribute_subgraph_packages_equivalent_subgraphs() -> None:
    # Two subgraphs declare the same single package: dependency-equivalent. One
    # owns the node, the other shows it as a duplicate, so both boxes render.
    subgraph_closures = [("cider", {"juice"}), ("wine", {"juice"})]
    direct_packages = {"cider": {"juice"}, "wine": {"juice"}}
    primary, duplicates = attribute_subgraph_packages(
        subgraph_closures, set(), direct_packages
    )
    assert primary["cider"] == {"juice"}
    assert primary["wine"] == set()
    assert duplicates["cider"] == set()
    assert duplicates["wine"] == {"juice"}


def test_render_mermaid_with_duplicate_headlines() -> None:
    root_name, nodes, edges = build_dependency_graph(SAMPLE_SBOM)
    # `pyyaml` is a shared headline: owned by extra `carapace`, duplicated into
    # extra `yaml` so both boxes render it (see attribute_subgraph_packages).
    extra_packages = {"carapace": {"pyyaml"}, "yaml": set()}
    extra_duplicates = {"carapace": set(), "yaml": {"pyyaml"}}
    extended_nodes = dict(nodes)
    extended_nodes["pyyaml-7@6.0.3"] = ("pyyaml", "6.0.3")
    extended_edges = list(edges) + [("my-project", "pyyaml")]
    lock_specs = LockSpecifiers(
        by_package={},
        by_subgraph={
            "carapace": {"pyyaml": ">=6.0.3"},
            "yaml": {"pyyaml": ">=6.0.3"},
        },
    )
    output = render_mermaid(
        root_name,
        extended_nodes,
        extended_edges,
        extra_packages=extra_packages,
        lock_specs=lock_specs,
        extra_duplicates=extra_duplicates,
    )
    # Owner renders the real node; the duplicate box uses a prefixed node ID so
    # Mermaid keeps it separate, with the same label and PyPI link.
    assert 'pyyaml{{"`pyyaml >=6.0.3`"}}' in output
    assert 'ext_yaml_pyyaml{{"`pyyaml >=6.0.3`"}}' in output
    # The duplicate-only box still renders and gets a dashed arrow from root.
    assert "subgraph ext_yaml [--extra yaml]" in output
    assert "my_project -.-> ext_yaml" in output
    # Duplicate node links to PyPI and gets the thick primary border.
    assert 'click ext_yaml_pyyaml "https://pypi.org/project/pyyaml/" _blank' in output
    assert "style ext_yaml_pyyaml stroke-width:3px" in output


def test_compute_node_degrees() -> None:
    _, _, edges = build_dependency_graph(SAMPLE_SBOM)
    degrees = _compute_node_degrees(edges)
    # requests: 1 out (from root) + 2 out (urllib3, certifi) = degree 3.
    assert degrees["requests"] == 3
    # click: 1 in (from root) = degree 1.
    assert degrees["click"] == 1
    # root: 2 out (click, requests) = degree 2.
    assert degrees["my-project"] == 2


def test_compute_subtree_sizes() -> None:
    _, _, edges = build_dependency_graph(SAMPLE_SBOM)
    subtree = _compute_subtree_sizes(edges)
    # requests has 2 descendants: urllib3 and certifi.
    assert subtree["requests"] == 2
    # click has 0 descendants.
    assert subtree["click"] == 0
    # Leaf nodes have 0 descendants.
    assert subtree["urllib3"] == 0
    assert subtree["certifi"] == 0


def test_compute_node_depths() -> None:
    root_name, _, edges = build_dependency_graph(SAMPLE_SBOM)
    depths = _compute_node_depths(root_name, edges)
    assert depths["my-project"] == 0
    assert depths["click"] == 1
    assert depths["requests"] == 1
    assert depths["urllib3"] == 2
    assert depths["certifi"] == 2


def test_cyclic_graph_traversals_terminate() -> None:
    """Every graph traversal must terminate on cyclic input.

    Without their visited guards, `_compute_node_depths` would loop forever and
    `_compute_subtree_sizes` would recurse infinitely on the apple <-> banana
    cycle, so reaching the asserts at all proves cycle-safety; the values pin the
    expected output.
    """
    root_name, nodes, edges = build_dependency_graph(CYCLIC_SBOM)
    assert root_name == "orchard"
    # The self-loop survives parsing as a (cherry, cherry) edge.
    assert ("cherry", "cherry") in edges
    assert ("apple", "banana") in edges
    assert ("banana", "apple") in edges

    # trim_graph_to_depth: BFS bounded by depth, neighbours visited once.
    depth2_nodes, depth2_edges = trim_graph_to_depth(root_name, nodes, edges, 2)
    assert {name for name, _ in depth2_nodes.values()} == {"orchard", "apple", "banana"}
    # cherry sits at depth 3, so it is trimmed out at depth 2.
    assert ("banana", "cherry") not in depth2_edges
    # A depth far beyond the graph keeps every node and edge without looping.
    full_nodes, full_edges = trim_graph_to_depth(root_name, nodes, edges, 100)
    assert {name for name, _ in full_nodes.values()} == {
        "orchard",
        "apple",
        "banana",
        "cherry",
    }
    assert len(full_edges) == len(edges)

    # filter_graph_to_package: fixed-point closure converges despite the cycle.
    filtered_nodes, filtered_edges = filter_graph_to_package(
        root_name, nodes, edges, "apple"
    )
    assert {name for name, _ in filtered_nodes.values()} == {
        "apple",
        "banana",
        "cherry",
    }
    # orchard -> apple is dropped: orchard is not reachable from apple.
    assert ("orchard", "apple") not in filtered_edges
    assert ("banana", "apple") in filtered_edges
    assert ("cherry", "cherry") in filtered_edges

    # _compute_node_depths: BFS assigns each node its shortest distance once.
    depths = _compute_node_depths(root_name, edges)
    assert depths == {"orchard": 0, "apple": 1, "banana": 2, "cherry": 3}

    # _compute_subtree_sizes: DFS counts reachable descendants, finite under
    # cycles. A node on a cycle appears in its own descendant set (apple reaches
    # itself via banana), and the self-loop makes cherry its own sole descendant.
    subtree = _compute_subtree_sizes(edges)
    assert subtree == {"orchard": 3, "apple": 3, "banana": 3, "cherry": 1}


def test_render_mermaid_with_cycle() -> None:
    """The full render path stays cycle-safe and emits the self-loop edge."""
    root_name, nodes, edges = build_dependency_graph(CYCLIC_SBOM)
    output = render_mermaid(root_name, nodes, edges)
    assert output.startswith("flowchart LR")
    assert "cherry --> cherry" in output
    assert "banana --> apple" in output


def test_render_mermaid_primary_deps_ordering() -> None:
    """Primary deps with larger subtrees should be declared first."""
    root_name, nodes, edges = build_dependency_graph(SAMPLE_SBOM)
    output = render_mermaid(root_name, nodes, edges)
    # requests (subtree=2) should appear before click (subtree=0).
    lines = output.splitlines()
    requests_idx = next(i for i, line in enumerate(lines) if "requests{{" in line)
    click_idx = next(i for i, line in enumerate(lines) if "click_0{{" in line)
    assert requests_idx < click_idx


def test_render_mermaid_edge_ordering() -> None:
    """Root edges should come before transitive edges."""
    root_name, nodes, edges = build_dependency_graph(SAMPLE_SBOM)
    output = render_mermaid(root_name, nodes, edges)
    lines = output.splitlines()
    # Find edge lines (contain ==> or -->).
    edge_lines = [line.strip() for line in lines if "==>" in line or "-->" in line]
    # Root edges (my_project ==>) should come before transitive edges.
    root_edge_indices = [
        i for i, line in enumerate(edge_lines) if line.startswith("my_project")
    ]
    transitive_edge_indices = [
        i for i, line in enumerate(edge_lines) if not line.startswith("my_project")
    ]
    if root_edge_indices and transitive_edge_indices:
        assert max(root_edge_indices) < min(transitive_edge_indices)

    # Among root edges, requests (degree=3) should come before click (degree=1).
    root_edges = [edge_lines[i] for i in root_edge_indices]
    requests_edge = next(i for i, line in enumerate(root_edges) if "requests" in line)
    click_edge = next(i for i, line in enumerate(root_edges) if "click_0" in line)
    assert requests_edge < click_edge


def test_get_available_groups() -> None:
    # Test against the actual pyproject.toml in the repo.
    groups = get_available_groups()
    # Should discover test and typing groups.
    assert "test" in groups
    assert "typing" in groups


def test_get_available_extras(tmp_path: Path) -> None:
    # Use a temporary pyproject.toml with known extras.
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[project.optional-dependencies]\n"
        'xml = ["click-extra [xml]"]\n'
        'yaml = ["click-extra [yaml]"]\n'
        'json5 = ["click-extra [json5]"]\n'
    )
    extras = get_available_extras(pyproject)
    assert "xml" in extras
    assert "yaml" in extras
    assert "json5" in extras
