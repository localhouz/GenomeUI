from __future__ import annotations

import unittest

import backend.main as main


class GraphWorldModelUnitTests(unittest.TestCase):
    def build_graph_fixture(self) -> tuple[dict[str, object], dict[str, object], dict[str, object], dict[str, object]]:
        graph = main.make_empty_graph()
        task_root = main.graph_add_entity(
            graph,
            {"kind": "task", "title": "Ship onboarding", "done": False, "createdAt": 3},
        )
        task_mid = main.graph_add_entity(
            graph,
            {"kind": "task", "title": "Validate policy", "done": False, "createdAt": 2},
        )
        task_leaf = main.graph_add_entity(
            graph,
            {"kind": "task", "title": "Archive checklist", "done": True, "createdAt": 1},
        )
        note = main.graph_add_entity(
            graph,
            {"kind": "note", "text": "onboarding status", "createdAt": 4},
        )
        main.graph_add_relation(graph, str(task_root["id"]), str(task_mid["id"]), "depends_on")
        main.graph_add_relation(graph, str(task_mid["id"]), str(task_leaf["id"]), "depends_on")
        main.graph_add_relation(graph, str(task_root["id"]), str(note["id"]), "references")
        return graph, task_root, task_mid, task_leaf

    def test_graph_schema_payload_includes_canonical_kinds(self) -> None:
        graph, *_ = self.build_graph_fixture()
        payload = main.build_graph_schema_payload(graph)
        self.assertIn("task", payload.get("entityKinds", []))
        self.assertIn("note", payload.get("entityKinds", []))
        self.assertIn("depends_on", payload.get("relationKinds", []))
        counts = payload.get("counts", {})
        self.assertEqual(int(counts.get("entities", 0)), 4)
        self.assertEqual(int(counts.get("relations", 0)), 3)

    def test_query_graph_filters_task_state_and_text(self) -> None:
        graph, *_ = self.build_graph_fixture()
        report = main.query_graph(graph, kind="task", text_query="ship", done=False, relation="depends_on", limit=10)
        entities = report.get("entities", [])
        relations = report.get("relations", [])
        self.assertEqual(len(entities), 1)
        self.assertIn("ship onboarding", str(entities[0].get("label", "")).lower())
        self.assertGreaterEqual(len(relations), 1)
        self.assertEqual(str(relations[0].get("kind", "")), "depends_on")

    def test_graph_neighborhood_and_path_reports(self) -> None:
        graph, root, _, leaf = self.build_graph_fixture()
        neighborhood = main.build_graph_neighborhood(graph, source=root, depth=2, relation="depends_on", limit=20)
        summary = neighborhood.get("summary", {})
        self.assertEqual(int(summary.get("depth", 0)), 2)
        self.assertGreaterEqual(int(summary.get("nodes", 0)), 3)
        self.assertGreaterEqual(int(summary.get("edges", 0)), 2)
        path_report = main.build_graph_path(graph, source=root, target=leaf, relation="depends_on", directed=True, limit=20)
        path_summary = path_report.get("summary", {})
        self.assertTrue(bool(path_summary.get("pathFound", False)))
        self.assertEqual(int(path_summary.get("pathLength", 0)), 2)
        reverse_report = main.build_graph_path(graph, source=leaf, target=root, relation="depends_on", directed=True, limit=20)
        self.assertFalse(bool((reverse_report.get("summary", {}) or {}).get("pathFound", True)))

    def test_graph_health_marks_dangling_relations_as_degraded(self) -> None:
        graph, root, *_ = self.build_graph_fixture()
        graph.setdefault("relations", []).append(
            {
                "id": "dangling01",
                "sourceId": str(root["id"]),
                "targetId": "missing-entity",
                "kind": "depends_on",
                "createdAt": 10,
            }
        )
        health = main.build_graph_health_report(graph)
        summary = health.get("summary", {})
        self.assertEqual(str(summary.get("status", "")), "degraded")
        self.assertGreaterEqual(int(summary.get("danglingRelations", 0)), 1)

    def test_local_plan_trace_includes_graph_snapshot(self) -> None:
        graph, *_ = self.build_graph_fixture()
        envelope = main.compile_intent_envelope("show graph summary")
        plan = main.build_local_plan(envelope, graph, {"ok": True, "message": "Graph ready"}, [])
        trace = plan.get("trace", {})
        snapshot = trace.get("graphSnapshot", {})
        self.assertIsInstance(snapshot, dict)
        self.assertGreaterEqual(len(snapshot.get("entities", [])), 1)
        self.assertGreaterEqual(len(snapshot.get("relations", [])), 1)
        self.assertEqual(int((snapshot.get("counts", {}) or {}).get("entities", 0)), 4)

    def test_enrich_plan_trace_adds_snapshot_to_remote_plan(self) -> None:
        graph, *_ = self.build_graph_fixture()
        remote_plan = {
            "version": "1.0.0",
            "title": "Remote",
            "subtitle": "Synthesized",
            "layout": {"columns": 2, "density": "normal"},
            "suggestions": ["show graph summary"],
            "blocks": [{"id": "sys", "type": "narrative", "label": "Workspace", "text": "ok"}],
            "trace": {"planVersion": "remote-v1", "focusDomains": ["graph"], "mode": "immersive"},
        }
        normalized = main.normalize_plan(remote_plan)
        enriched = main.enrich_plan_trace(normalized, graph, {"target": "ollama-small", "reason": "single-domain"})
        snapshot = ((enriched.get("trace", {}) or {}).get("graphSnapshot", {}) or {})
        self.assertGreaterEqual(len(snapshot.get("entities", [])), 1)
        self.assertGreaterEqual(len(snapshot.get("relations", [])), 1)
        self.assertEqual(str((enriched.get("trace", {}) or {}).get("routeTarget", "")), "ollama-small")


if __name__ == "__main__":
    unittest.main()
