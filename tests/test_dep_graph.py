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

"""Tests for `repomatic.dep_graph`: the Mermaid dependency-graph generator."""

from __future__ import annotations

from pathlib import Path

import pytest

from repomatic.dep_graph import (
    Subgraph,
    SubgraphKind,
    _compute_node_degrees,
    _compute_node_depths,
    _compute_subtree_sizes,
    attribute_subgraph_packages,
    build_dependency_graph,
    filter_graph_to_package,
    filter_root_edges,
    normalize_package_name,
    render_mermaid,
    resolve_subgraph_selection,
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


def _subgraph_block(output: str, subgraph_id: str) -> list[str]:
    """Extract the lines between a subgraph declaration and its closing `end`."""
    lines = output.splitlines()
    start = next(
        i
        for i, line in enumerate(lines)
        if line.strip().startswith(f"subgraph {subgraph_id} ")
    )
    end = next(i for i in range(start, len(lines)) if lines[i].strip() == "end")
    return [line.strip() for line in lines[start + 1 : end]]


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
    ("kind", "name", "mermaid_id", "title"),
    [
        (SubgraphKind.GROUP, "test", "grp_test", "--group test"),
        (SubgraphKind.EXTRA, "json5", "ext_json5", "--extra json5"),
    ],
)
def test_subgraph_identity(
    kind: SubgraphKind, name: str, mermaid_id: str, title: str
) -> None:
    subgraph = Subgraph(kind, name, set(), set())
    assert subgraph.mermaid_id == mermaid_id
    assert subgraph.title == title


def test_build_dependency_graph() -> None:
    root_name, packages, edges = build_dependency_graph(SAMPLE_SBOM)

    assert root_name == "my-project"
    assert packages == {"my-project", "click", "requests", "urllib3", "certifi"}

    # Check edges.
    assert ("my-project", "click") in edges
    assert ("my-project", "requests") in edges
    assert ("requests", "urllib3") in edges
    assert ("requests", "certifi") in edges
    assert len(edges) == 4


def test_filter_root_edges_drops_group_package_pulled_in_by_an_extra() -> None:
    """A group package an extra drags in is not a main dependency.

    uv's CycloneDX export hangs `requests` off the root because the `test`
    group declares it, though only the `sphinx` extra pulls it in. Exporting
    with no `--group` at all still produces that edge.
    """
    subgraphs = [Subgraph(SubgraphKind.EXTRA, "sphinx", {"sphinx"}, set())]
    edges = [
        ("my-project", "click"),
        ("my-project", "sphinx"),
        ("my-project", "requests"),
        ("sphinx", "requests"),
        ("requests", "urllib3"),
    ]

    filtered = filter_root_edges("my-project", edges, {"click"}, subgraphs)

    assert ("my-project", "requests") not in filtered
    # The package keeps hanging off whatever really pulls it in.
    assert ("sphinx", "requests") in filtered
    assert ("requests", "urllib3") in filtered
    # Both kinds of backed root edge survive: declared main, and box-owned.
    assert ("my-project", "click") in filtered
    assert ("my-project", "sphinx") in filtered


def test_filter_root_edges_without_lock_data() -> None:
    """Missing lock data is not evidence that a root edge is spurious."""
    edges = [("my-project", "click"), ("my-project", "requests")]
    assert filter_root_edges("my-project", edges, None, []) == edges


def test_filter_root_edges_with_no_main_dependencies() -> None:
    """A project declaring nothing unconditionally keeps only box-owned edges.

    An empty mapping states that much, where `None` states nothing at all.
    """
    subgraphs = [Subgraph(SubgraphKind.EXTRA, "xml", {"xmltodict"}, set())]
    edges = [("my-project", "xmltodict"), ("my-project", "requests")]

    filtered = filter_root_edges("my-project", edges, set(), subgraphs)

    assert filtered == [("my-project", "xmltodict")]


def test_render_mermaid_after_filtering_root_edges() -> None:
    """The dropped package renders as a transitive oval, outside every box."""
    subgraphs = [Subgraph(SubgraphKind.EXTRA, "sphinx", {"sphinx"}, set())]
    edges = filter_root_edges(
        "my-project",
        [
            ("my-project", "click"),
            ("my-project", "sphinx"),
            ("my-project", "requests"),
            ("sphinx", "requests"),
        ],
        {"click"},
        subgraphs,
    )
    # by_package still carries the test group's specifier, since a group box
    # needs it to label its own edges. Nothing must pick it up here.
    lock_specs = LockSpecifiers(
        by_package={"my-project": {"click": ">=8.0", "requests": ">=2.34"}},
        by_subgraph={},
    )

    output = render_mermaid(
        "my-project",
        {"my-project", "click", "sphinx", "requests"},
        edges,
        subgraphs,
        lock_specs,
    )

    assert 'requests(["`requests`"])' in output
    assert "sphinx --> requests" in output
    # No thick root arrow, no primary hexagon, no group specifier leaking in.
    assert "my_project ==> requests" not in output
    assert ">=2.34" not in output
    assert not any(
        "requests" in line for line in _subgraph_block(output, "primary-deps")
    )


def test_filter_graph_to_package() -> None:
    _root_name, packages, edges = build_dependency_graph(SAMPLE_SBOM)

    # Filter to requests package.
    filtered_packages, filtered_edges = filter_graph_to_package(
        packages, edges, "requests"
    )

    # Should only include requests and its dependencies.
    assert filtered_packages == {"requests", "urllib3", "certifi"}

    # Check edges.
    assert ("requests", "urllib3") in filtered_edges
    assert ("requests", "certifi") in filtered_edges
    assert len(filtered_edges) == 2


@pytest.mark.parametrize(
    ("depth", "packages", "edge_count"),
    (
        pytest.param(0, {"my-project"}, 0, id="root-only"),
        pytest.param(1, {"my-project", "click", "requests"}, 2, id="primary-deps"),
        pytest.param(
            2,
            {"my-project", "click", "requests", "urllib3", "certifi"},
            4,
            id="transitive-deps",
        ),
        # A depth beyond the graph keeps everything rather than erroring.
        pytest.param(
            100,
            {"my-project", "click", "requests", "urllib3", "certifi"},
            4,
            id="past-the-end",
        ),
    ),
)
def test_trim_graph_to_depth(depth, packages, edge_count) -> None:
    """Trimming keeps every node within *depth* hops of the root, and no more."""
    root_name, all_packages, edges = build_dependency_graph(SAMPLE_SBOM)
    trimmed_packages, trimmed_edges = trim_graph_to_depth(
        root_name, all_packages, edges, depth
    )
    assert trimmed_packages == packages
    assert len(trimmed_edges) == edge_count


def test_render_mermaid() -> None:
    root_name, packages, edges = build_dependency_graph(SAMPLE_SBOM)
    output = render_mermaid(root_name, packages, edges)

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
    root_name, packages, edges = build_dependency_graph(SAMPLE_SBOM)
    # Provide specifiers for edge labels.
    lock_specs = LockSpecifiers(
        by_package={
            "my-project": {"click": ">=8.0", "requests": ">=2.28"},
            "requests": {"urllib3": ">=1.26,<2", "certifi": ">=2022"},
        },
        by_subgraph={},
    )
    output = render_mermaid(root_name, packages, edges, lock_specs=lock_specs)

    # Check edge labels with specifiers.
    assert 'my_project ==>|" >=8.0 "| click_0' in output
    assert 'my_project ==>|" >=2.28 "| requests' in output
    assert 'requests -->|" >=1.26,<2 "| urllib3' in output
    assert 'requests -->|" >=2022 "| certifi' in output


def test_render_mermaid_with_groups() -> None:
    root_name, packages, edges = build_dependency_graph(SAMPLE_SBOM)
    # Add a test package declared by a group.
    subgraphs = [Subgraph(SubgraphKind.GROUP, "test", {"pytest"}, set())]
    extended_packages = packages | {"pytest"}
    extended_edges = list(edges) + [("my-project", "pytest")]

    output = render_mermaid(root_name, extended_packages, extended_edges, subgraphs)

    # Group subgraph ID is prefixed to avoid collision with node IDs.
    assert "subgraph grp_test [--group test]" in output
    # Root uses dashed arrow to group subgraph, not an individual edge.
    assert "my_project -.-> grp_test" in output
    assert "my_project ==> pytest" not in output


def test_render_mermaid_with_subgraph_specifiers() -> None:
    root_name, packages, edges = build_dependency_graph(SAMPLE_SBOM)
    # Add test packages declared by a group.
    subgraphs = [Subgraph(SubgraphKind.GROUP, "test", {"pytest", "coverage"}, set())]
    extended_packages = packages | {"pytest", "coverage"}
    extended_edges = list(edges) + [
        ("my-project", "pytest"),
        ("my-project", "coverage"),
    ]
    # Specifiers for the declared deps of the test group.
    lock_specs = LockSpecifiers(
        by_package={},
        by_subgraph={"test": {"pytest": ">=9", "coverage": ">=7.11"}},
    )

    output = render_mermaid(
        root_name,
        extended_packages,
        extended_edges,
        subgraphs,
        lock_specs=lock_specs,
    )

    # Declared group deps use hexagon shape with specifier in label.
    assert 'pytest{{"`pytest >=9`"}}' in output
    assert 'coverage{{"`coverage >=7.11`"}}' in output


def test_render_mermaid_transitive_deps_outside_box() -> None:
    root_name, packages, edges = build_dependency_graph(SAMPLE_SBOM)
    # pytest and pytest-cov are declared by the group; iniconfig is only pulled
    # in transitively, so it must render outside the box like any other
    # transitive dependency.
    subgraphs = [Subgraph(SubgraphKind.GROUP, "test", {"pytest", "pytest-cov"}, set())]
    extended_packages = packages | {"pytest", "pytest-cov", "iniconfig"}
    extended_edges = list(edges) + [
        ("my-project", "pytest"),
        ("my-project", "pytest-cov"),
        ("pytest-cov", "pytest"),
        ("pytest", "iniconfig"),
    ]
    lock_specs = LockSpecifiers(
        by_package={},
        by_subgraph={"test": {"pytest": ">=9", "pytest-cov": ">=7"}},
    )

    output = render_mermaid(
        root_name,
        extended_packages,
        extended_edges,
        subgraphs,
        lock_specs=lock_specs,
    )

    # Declared deps use hexagon shape, inside the box.
    box_lines = _subgraph_block(output, "grp_test")
    assert 'pytest{{"`pytest >=9`"}}' in box_lines
    assert 'pytest_cov{{"`pytest-cov >=7`"}}' in box_lines
    # The transitive dep renders outside the box, as a plain oval.
    assert all("iniconfig" not in line for line in box_lines)
    assert 'iniconfig(["`iniconfig`"])' in output
    # Declared deps get the thick border; the transitive dep does not.
    assert "style pytest stroke-width:3px" in output
    assert "style iniconfig" not in output
    # A transitive edge between two non-root packages stays thin, even when it
    # points at a declared dep: only edges leaving the root are thick.
    assert "pytest_cov --> pytest" in output
    # Arrow pointing to a transitive dep uses normal style.
    assert "pytest --> iniconfig" in output


def test_render_mermaid_with_extras() -> None:
    root_name, packages, edges = build_dependency_graph(SAMPLE_SBOM)
    # Add a package declared by an extra.
    subgraphs = [Subgraph(SubgraphKind.EXTRA, "xml", {"lxml"}, set())]
    extended_packages = packages | {"lxml"}
    extended_edges = list(edges) + [("my-project", "lxml")]

    output = render_mermaid(root_name, extended_packages, extended_edges, subgraphs)

    # Extra subgraph ID is prefixed to avoid collision with node IDs.
    assert "subgraph ext_xml [--extra xml]" in output
    # Root uses dashed arrow to extra subgraph.
    assert "my_project -.-> ext_xml" in output


def test_render_mermaid_skips_filtered_out_box() -> None:
    # The group's packages were all filtered out of the graph (depth trim or
    # package focus): the box, its dashed arrow, and its style line all vanish.
    root_name, packages, edges = build_dependency_graph(SAMPLE_SBOM)
    subgraphs = [Subgraph(SubgraphKind.GROUP, "test", {"pytest"}, set())]

    output = render_mermaid(root_name, packages, edges, subgraphs)

    assert "grp_test" not in output
    assert "pytest" not in output


def test_attribute_subgraph_packages_duplicate_headlines() -> None:
    # Three subgraphs share the directly-declared package `sugar`; `water` is a
    # base dependency excluded from every box. No edges, so all dependent
    # counts tie at zero and declaration order picks the owner.
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
    owned, duplicates = attribute_subgraph_packages(
        subgraph_closures, {"water"}, direct_packages, [], "pantry"
    )
    # First declarer (processing order) owns the shared headline as a real node.
    assert owned["bakery"] == {"flour", "sugar"}
    assert owned["cafe"] == set()
    assert owned["diner"] == {"plate"}
    # Base dependency never lands in a subgraph.
    assert all("water" not in pkgs for pkgs in owned.values())
    # Siblings that also declare `sugar` keep it as a duplicate headline, so
    # their box still renders the dependency they exist to install.
    assert duplicates["bakery"] == set()
    assert duplicates["cafe"] == {"sugar"}
    assert duplicates["diner"] == {"sugar"}


def test_attribute_subgraph_packages_equivalent_subgraphs() -> None:
    # Two subgraphs declare the same single package: dependency-equivalent. One
    # owns the node, the other shows it as a duplicate, so both boxes render.
    # Root edges never count as dependents, so ownership falls back to
    # declaration order despite the root depending on `juice`.
    subgraph_closures = [("cider", {"juice"}), ("wine", {"juice"})]
    direct_packages = {"cider": {"juice"}, "wine": {"juice"}}
    owned, duplicates = attribute_subgraph_packages(
        subgraph_closures, set(), direct_packages, [("orchard", "juice")], "orchard"
    )
    assert owned["cider"] == {"juice"}
    assert owned["wine"] == set()
    assert duplicates["cider"] == set()
    assert duplicates["wine"] == {"juice"}


def test_attribute_subgraph_packages_dependents_tie_break() -> None:
    # Both subgraphs declare `yeast` directly, but only `bread`'s closure holds
    # packages depending on it: `bread` wins the real node even though `pastry`
    # declares it first, and `pastry` keeps a duplicate headline instead.
    subgraph_closures = [
        ("pastry", {"butter", "yeast"}),
        ("bread", {"sourdough", "baguette", "yeast"}),
    ]
    direct_packages = {
        "pastry": {"butter", "yeast"},
        "bread": {"sourdough", "baguette", "yeast"},
    }
    edges = [
        ("kitchen", "butter"),
        ("kitchen", "yeast"),
        ("kitchen", "sourdough"),
        ("kitchen", "baguette"),
        ("sourdough", "yeast"),
        ("baguette", "yeast"),
    ]
    owned, duplicates = attribute_subgraph_packages(
        subgraph_closures, set(), direct_packages, edges, "kitchen"
    )
    assert owned["pastry"] == {"butter"}
    assert owned["bread"] == {"sourdough", "baguette", "yeast"}
    assert duplicates["pastry"] == {"yeast"}
    assert duplicates["bread"] == set()


def test_attribute_subgraph_packages_transitive_stays_out() -> None:
    # `yeast` is pulled in by bakery's declared `flour` but is not declared
    # itself: it must not land in any box, nor count as a duplicate.
    subgraph_closures = [("bakery", {"flour", "yeast"})]
    direct_packages = {"bakery": {"flour"}}
    owned, duplicates = attribute_subgraph_packages(
        subgraph_closures, set(), direct_packages, [("flour", "yeast")], "pantry"
    )
    assert owned["bakery"] == {"flour"}
    assert duplicates["bakery"] == set()


def test_render_mermaid_with_duplicate_headlines() -> None:
    root_name, packages, edges = build_dependency_graph(SAMPLE_SBOM)
    # `pyyaml` is a shared headline: owned by extra `carapace`, duplicated into
    # extra `yaml` so both boxes render it (see attribute_subgraph_packages).
    subgraphs = [
        Subgraph(SubgraphKind.EXTRA, "carapace", {"pyyaml"}, set()),
        Subgraph(SubgraphKind.EXTRA, "yaml", set(), {"pyyaml"}),
    ]
    extended_packages = packages | {"pyyaml"}
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
        extended_packages,
        extended_edges,
        subgraphs,
        lock_specs=lock_specs,
    )
    # Owner renders the real node; the duplicate box uses a prefixed node ID so
    # Mermaid keeps it separate, with the same label and PyPI link.
    assert 'pyyaml{{"`pyyaml >=6.0.3`"}}' in output
    assert 'ext_yaml_pyyaml{{"`pyyaml >=6.0.3`"}}' in output
    # The duplicate-only box still renders and gets a dashed arrow from root.
    assert "subgraph ext_yaml [--extra yaml]" in output
    assert "my_project -.-> ext_yaml" in output
    # A dotted arrowless identity link ties the duplicate to its real node.
    assert "ext_yaml_pyyaml -.- pyyaml" in output
    # Duplicate node links to PyPI and gets a dashed thick border, while the
    # real node keeps the solid primary border.
    assert 'click ext_yaml_pyyaml "https://pypi.org/project/pyyaml/" _blank' in output
    assert "style ext_yaml_pyyaml stroke-width:3px,stroke-dasharray:5 5" in output
    assert "style pyyaml stroke-width:3px\n" in output


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
    root_name, packages, edges = build_dependency_graph(CYCLIC_SBOM)
    assert root_name == "orchard"
    # The self-loop survives parsing as a (cherry, cherry) edge.
    assert ("cherry", "cherry") in edges
    assert ("apple", "banana") in edges
    assert ("banana", "apple") in edges

    # trim_graph_to_depth: BFS bounded by depth, neighbours visited once.
    depth2_packages, depth2_edges = trim_graph_to_depth(root_name, packages, edges, 2)
    assert depth2_packages == {"orchard", "apple", "banana"}
    # cherry sits at depth 3, so it is trimmed out at depth 2.
    assert ("banana", "cherry") not in depth2_edges
    # A depth far beyond the graph keeps every node and edge without looping.
    full_packages, full_edges = trim_graph_to_depth(root_name, packages, edges, 100)
    assert full_packages == {"orchard", "apple", "banana", "cherry"}
    assert len(full_edges) == len(edges)

    # filter_graph_to_package: fixed-point closure converges despite the cycle.
    filtered_packages, filtered_edges = filter_graph_to_package(
        packages, edges, "apple"
    )
    assert filtered_packages == {"apple", "banana", "cherry"}
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
    root_name, packages, edges = build_dependency_graph(CYCLIC_SBOM)
    output = render_mermaid(root_name, packages, edges)
    assert output.startswith("flowchart LR")
    assert "cherry --> cherry" in output
    assert "banana --> apple" in output


def test_render_mermaid_primary_deps_ordering() -> None:
    """Primary deps with larger subtrees should be declared first."""
    root_name, packages, edges = build_dependency_graph(SAMPLE_SBOM)
    output = render_mermaid(root_name, packages, edges)
    # requests (subtree=2) should appear before click (subtree=0).
    lines = output.splitlines()
    requests_idx = next(i for i, line in enumerate(lines) if "requests{{" in line)
    click_idx = next(i for i, line in enumerate(lines) if "click_0{{" in line)
    assert requests_idx < click_idx


def test_render_mermaid_edge_ordering() -> None:
    """Root edges should come before transitive edges."""
    root_name, packages, edges = build_dependency_graph(SAMPLE_SBOM)
    output = render_mermaid(root_name, packages, edges)
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


def test_available_groups() -> None:
    # Test against the actual pyproject.toml in the repo.
    groups = SubgraphKind.GROUP.available()
    # Should discover test and typing groups.
    assert "test" in groups
    assert "typing" in groups


def test_available_extras(tmp_path: Path) -> None:
    # Use a temporary pyproject.toml with known extras.
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[project.optional-dependencies]\n"
        'xml = ["click-extra [xml]"]\n'
        'yaml = ["click-extra [yaml]"]\n'
        'json5 = ["click-extra [json5]"]\n',
        encoding="UTF-8",
    )
    assert SubgraphKind.EXTRA.available(tmp_path) == ("json5", "xml", "yaml")
    # The same file declares no dependency groups.
    assert SubgraphKind.GROUP.available(tmp_path) == ()


def test_resolve_subgraph_selection() -> None:
    # Resolve against the actual pyproject.toml in the repo, whose groups
    # include docs, test and typing.
    groups = SubgraphKind.GROUP.available()
    assert {"docs", "test", "typing"} <= set(groups)

    def resolve(
        explicit: tuple[str, ...] = (),
        select_all: bool = False,
        excluded: tuple[str, ...] = (),
        only: tuple[str, ...] = (),
        config_all: bool = False,
        config_excluded: tuple[str, ...] = (),
    ) -> tuple[str, ...] | None:
        return resolve_subgraph_selection(
            SubgraphKind.GROUP,
            explicit,
            select_all,
            excluded,
            only,
            config_all,
            config_excluded,
        )

    # No flags and no config default: the axis is not requested.
    assert resolve() is None
    # No flags: the config default expands to every declared group.
    assert resolve(config_all=True) == groups
    # An explicit selection wins over the config default.
    assert resolve(explicit=("test",), config_all=True) == ("test",)
    # --only-* replaces the explicit selection.
    assert resolve(explicit=("test",), only=("typing",)) == ("typing",)
    # --no-* prunes the expanded selection.
    assert resolve(select_all=True, excluded=("docs",)) == tuple(
        name for name in groups if name != "docs"
    )
    # The configured exclusions apply when no --no-* flag is passed.
    assert resolve(select_all=True, config_excluded=("docs", "typing")) == tuple(
        name for name in groups if name not in {"docs", "typing"}
    )
