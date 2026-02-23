from __future__ import annotations

import unittest

import backend.main as main


class GraphDependenciesUnitTests(unittest.TestCase):
    def build_dependency_graph(self) -> tuple[dict[str, object], dict[str, object], dict[str, object], dict[str, object]]:
        graph = main.make_empty_graph()
        root = main.graph_add_entity(graph, {"kind": "task", "title": "Root", "done": False, "createdAt": 1})
        mid = main.graph_add_entity(graph, {"kind": "task", "title": "Mid", "done": False, "createdAt": 2})
        leaf = main.graph_add_entity(graph, {"kind": "task", "title": "Leaf", "done": False, "createdAt": 3})
        note = main.graph_add_entity(graph, {"kind": "note", "text": "Reference doc", "createdAt": 4})
        main.graph_add_relation(graph, str(root["id"]), str(mid["id"]), "depends_on")
        main.graph_add_relation(graph, str(mid["id"]), str(leaf["id"]), "depends_on")
        main.graph_add_relation(graph, str(note["id"]), str(root["id"]), "references")
        return graph, root, mid, leaf

    def test_parse_relation_query_modes(self) -> None:
        dep = main.parse_relation_query("show dependencies for task 1")
        chain = main.parse_relation_query("show dependency chain for task 1")
        blockers = main.parse_relation_query("show blockers for task 1")
        impact = main.parse_relation_query("show impact for task 1")
        refs = main.parse_relation_query("show references for note 1")
        self.assertEqual(str(dep.get("mode", "")), "dependencies")
        self.assertEqual(str(chain.get("mode", "")), "dependency_chain")
        self.assertEqual(str(blockers.get("mode", "")), "blockers")
        self.assertEqual(str(impact.get("mode", "")), "impact")
        self.assertEqual(str(refs.get("mode", "")), "references")

    def test_dependency_chain_and_impact(self) -> None:
        graph, root, _, leaf = self.build_dependency_graph()
        chain_lines = main.dependency_chain_lines(graph, root)
        self.assertTrue(any("chain depth: 2" in str(line).lower() for line in chain_lines))
        impacted_ids = main.transitive_dependency_impact_ids(graph, str(leaf["id"]))
        self.assertEqual(len(impacted_ids), 2)

    def test_dependency_analysis_summary(self) -> None:
        graph, root, _, leaf = self.build_dependency_graph()
        root_summary = main.dependency_analysis_for_task(graph, root)
        leaf_summary = main.dependency_analysis_for_task(graph, leaf)
        self.assertEqual(int((root_summary.get("blockers") or {}).get("count", 0)), 1)
        self.assertEqual(int((leaf_summary.get("impact") or {}).get("count", 0)), 2)


if __name__ == "__main__":
    unittest.main()
