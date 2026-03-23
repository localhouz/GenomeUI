from __future__ import annotations

import asyncio
import pathlib
import tempfile
import unittest

import backend.main as main


class ContentRepoUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self._content_store_before = main.CONTENT_STORE_PATH
        self._sessions_before = dict(main.SESSIONS)
        self._tmpdir = tempfile.TemporaryDirectory()
        main.CONTENT_STORE_PATH = pathlib.Path(self._tmpdir.name) / "content_store.db"
        main._content_db().close()

    def tearDown(self) -> None:
        main.CONTENT_STORE_PATH = self._content_store_before
        main.SESSIONS.clear()
        main.SESSIONS.update(self._sessions_before)
        self._tmpdir.cleanup()

    def test_commit_load_and_history_are_repo_backed(self) -> None:
        first = main.content_commit("document", "Roadmap", "<h1>v1</h1>", message="Initial draft")
        second = main.content_commit("document", "Roadmap", "<h1>v2</h1>", message="Polish intro")

        loaded = main.content_load("document", "Roadmap")
        history = main.content_history("document", "Roadmap")

        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded["hash"], second["hash"])
        self.assertEqual(loaded["data"], "<h1>v2</h1>")
        self.assertTrue(bool(loaded.get("authoritative")))
        self.assertTrue(bool(loaded.get("connected")))
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["hash"], second["hash"])
        self.assertEqual(history[1]["hash"], first["hash"])
        self.assertTrue(bool(history[0]["current"]))

    def test_content_list_can_match_current_head_content(self) -> None:
        main.content_commit("document", "Kernel Roadmap", "<h1>Memory substrate</h1><p>Persistent runtime shell</p>", message="Seed")

        items = main.content_list("document", query="runtime shell")

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "Kernel Roadmap")
        self.assertIn("Persistent runtime shell", items[0]["summary"])
        self.assertIn("revisionCount", items[0])
        self.assertIn("branchCount", items[0])
        self.assertIn("attachedSessions", items[0])
        self.assertIn("headMessage", items[0])

    def test_branch_and_revert_create_new_repo_heads(self) -> None:
        original = main.content_commit("document", "Spec", "<h1>alpha</h1>", message="Initial")
        updated = main.content_commit("document", "Spec", "<h1>beta</h1>", message="Update")

        branched = main.content_branch_create("document", "Spec", "draft", from_branch="main")
        self.assertIsNotNone(branched)
        assert branched is not None
        self.assertEqual(branched["hash"], updated["hash"])

        reverted = main.content_revert("document", "Spec", original["hash"], branch="main")
        self.assertIsNotNone(reverted)
        assert reverted is not None
        self.assertEqual(reverted["data"], "<h1>alpha</h1>")
        self.assertNotEqual(reverted["hash"], updated["hash"])
        self.assertEqual(main.content_load("document", "Spec")["hash"], reverted["hash"])

    def test_branch_merge_advances_target_branch_head(self) -> None:
        main.content_commit("document", "Merge Spec", "<h1>main</h1>", message="Seed")
        main.content_branch_create("document", "Merge Spec", "draft", from_branch="main")
        draft = main.content_commit("document", "Merge Spec", "<h1>draft</h1>", branch="draft", message="Draft update")

        merged = main.content_merge("document", "Merge Spec", "draft", target_branch="main")

        self.assertIsNotNone(merged)
        assert merged is not None
        self.assertEqual(merged["branch"], "main")
        self.assertEqual(merged["data"], "<h1>draft</h1>")
        self.assertNotEqual(merged["hash"], draft["hash"])
        loaded = main.content_load("document", "Merge Spec", branch="main")
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded["hash"], merged["hash"])
        self.assertEqual(loaded["data"], "<h1>draft</h1>")

    def test_session_attach_tracks_workspace_without_storing_document_blob_in_session(self) -> None:
        item = main.content_commit("document", "Inbox Zero", "<p>clean slate</p>", session_id="sess-repo")

        session = main.ensure_session("sess-repo")
        workspace = session.workspace
        active = workspace.get("activeContent") if isinstance(workspace, dict) else {}

        self.assertEqual(active.get("itemId"), item["itemId"])
        self.assertEqual(active.get("name"), "Inbox Zero")
        self.assertEqual(active.get("domain"), "document")
        self.assertNotIn("data", active)

        worktree = (workspace.get("worktrees") or {}).get(item["itemId"], {})
        self.assertEqual(worktree.get("branch"), "main")
        self.assertEqual(worktree.get("itemId"), item["itemId"])
        self.assertEqual(worktree.get("domain"), "document")

    def test_open_operation_sets_active_content_from_repo(self) -> None:
        item = main.content_commit("document", "Kernel Notes", "<p>boot graph</p>", message="Seed document")
        session = main.ensure_session("sess-open")

        result = main.run_operation(session, {"type": "document_open", "payload": {"name": "Kernel Notes"}})

        self.assertTrue(bool(result.get("ok")))
        self.assertEqual(result.get("data", {}).get("hash"), item["hash"])
        active = session.workspace.get("activeContent") if isinstance(session.workspace, dict) else {}
        self.assertEqual(active.get("itemId"), item["itemId"])
        self.assertEqual(active.get("name"), "Kernel Notes")
        self.assertEqual(active.get("domain"), "document")

    def test_branch_create_api_helper_preserves_head_for_new_branch(self) -> None:
        main.content_commit("document", "Branch Spec", "<p>mainline</p>", message="Seed")

        branched = main.content_branch_create("document", "Branch Spec", "draft", from_branch="main")

        self.assertIsNotNone(branched)
        assert branched is not None
        self.assertEqual(branched["branch"], "draft")
        loaded = main.content_load("document", "Branch Spec", branch="draft")
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded["branch"], "draft")
        self.assertEqual(loaded["data"], "<p>mainline</p>")

    def test_branch_listing_includes_default_and_new_branch(self) -> None:
        seeded = main.content_commit("document", "Repo View", "<p>seed</p>", message="Seed")
        main.content_branch_create("document", "Repo View", "draft", from_branch="main")

        branches = main.content_branches("document", "Repo View")

        self.assertGreaterEqual(len(branches), 2)
        names = {row["branch"] for row in branches}
        self.assertIn("main", names)
        self.assertIn("draft", names)
        main_row = next(row for row in branches if row["branch"] == "main")
        self.assertTrue(bool(main_row["isDefault"]))
        self.assertEqual(main_row["hash"], seeded["hash"])

    def test_session_worktree_activation_and_detach(self) -> None:
        first = main.content_commit("document", "Alpha", "<p>a</p>", session_id="sess-wt")
        second = main.content_commit("document", "Beta", "<p>b</p>", session_id="sess-wt")

        listed = main.content_session_worktrees("sess-wt")
        self.assertEqual(len(listed), 2)

        activated = main.content_activate_session_worktree("sess-wt", first["itemId"])
        self.assertIsNotNone(activated)
        assert activated is not None
        session = main.ensure_session("sess-wt")
        active = session.workspace.get("activeContent") if isinstance(session.workspace, dict) else {}
        self.assertEqual(active.get("itemId"), first["itemId"])

        detached = main.content_detach_session_worktree("sess-wt", second["itemId"])
        self.assertTrue(detached)
        listed_after = main.content_session_worktrees("sess-wt")
        ids_after = {row["itemId"] for row in listed_after}
        self.assertIn(first["itemId"], ids_after)
        self.assertNotIn(second["itemId"], ids_after)

    def test_attach_existing_content_to_session_worktree(self) -> None:
        seeded = main.content_commit("document", "Gamma", "<p>g</p>", message="Seed")

        attached = main.content_attach_existing_to_session("sess-attach", "document", "Gamma", branch="main")

        self.assertIsNotNone(attached)
        assert attached is not None
        listed = main.content_session_worktrees("sess-attach")
        ids = {row["itemId"] for row in listed}
        self.assertIn(seeded["itemId"], ids)

    def test_worktree_lifecycle_operations_run_through_kernel(self) -> None:
        seeded = main.content_commit("document", "Delta", "<p>d</p>", message="Seed")
        session = main.ensure_session("sess-ops")

        attach = main.run_operation(session, {"type": "content_attach", "payload": {"type": "document", "name": "Delta", "branch": "main"}})
        self.assertTrue(bool(attach.get("ok")))

        activate = main.run_operation(session, {"type": "content_activate", "payload": {"itemId": seeded["itemId"]}})
        self.assertTrue(bool(activate.get("ok")))
        self.assertEqual(session.workspace.get("activeContent", {}).get("itemId"), seeded["itemId"])

        detach = main.run_operation(session, {"type": "content_detach", "payload": {"itemId": seeded["itemId"]}})
        self.assertTrue(bool(detach.get("ok")))
        self.assertEqual(main.content_session_worktrees("sess-ops"), [])

    def test_content_merge_runs_through_kernel(self) -> None:
        main.content_commit("document", "Kernel Merge", "<p>main</p>", message="Seed")
        main.content_branch_create("document", "Kernel Merge", "draft", from_branch="main")
        main.content_commit("document", "Kernel Merge", "<p>draft</p>", branch="draft", message="Draft")
        session = main.ensure_session("sess-merge")

        merged = main.run_operation(
            session,
            {"type": "content_merge", "payload": {"type": "document", "name": "Kernel Merge", "branch": "main", "sourceBranch": "draft"}},
        )

        self.assertTrue(bool(merged.get("ok")))
        data = merged.get("data", {})
        self.assertEqual(data.get("branch"), "main")
        self.assertEqual(session.workspace.get("activeContent", {}).get("name"), "Kernel Merge")

    def test_runtime_notifications_are_session_backed(self) -> None:
        session = main.ensure_session("sess-notifs")
        main.record_runtime_notification(
            session,
            notif_type="reminder",
            title="Stretch",
            message="Stand up in 10m",
            route="start stretch routine",
            created_at=123456,
        )

        viewed = main.run_operation(session, {"type": "notifications_view", "payload": {}})

        self.assertTrue(bool(viewed.get("ok")))
        notifications = viewed.get("data", {}).get("notifications", [])
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0]["title"], "Stretch")
        self.assertTrue(bool(viewed.get("data", {}).get("authoritative")))
        sync_payload = main.session_sync_payload("sess-notifs", session)
        self.assertEqual(sync_payload.get("workspace"), session.workspace)
        self.assertEqual(len(sync_payload.get("notifications", [])), 1)

    def test_notification_ops_mutate_runtime_inbox(self) -> None:
        session = main.ensure_session("sess-notif-ops")
        main.record_runtime_notification(session, notif_type="gmail", title="Gmail", message="Inbox changed", created_at=100)
        main.record_runtime_notification(session, notif_type="slack", title="Slack", message="Mentioned", created_at=200)

        marked = main.run_operation(session, {"type": "notifications_mark_read", "payload": {"app": "gmail"}})
        self.assertTrue(bool(marked.get("ok")))
        gmail_entry = next(item for item in session.notifications if item["title"] == "Gmail")
        slack_entry = next(item for item in session.notifications if item["title"] == "Slack")
        self.assertTrue(bool(gmail_entry["read"]))
        self.assertFalse(bool(slack_entry["read"]))

        cleared = main.run_operation(session, {"type": "notifications_clear_app", "payload": {"app": "slack"}})
        self.assertTrue(bool(cleared.get("ok")))
        titles = {item["title"] for item in session.notifications}
        self.assertIn("Gmail", titles)
        self.assertNotIn("Slack", titles)

    def test_connections_status_payload_is_shell_ready(self) -> None:
        payload = main.build_connections_status_payload("gmail")

        self.assertTrue(bool(payload.get("ok")))
        data = payload.get("data", {})
        self.assertEqual(data.get("targetService"), "gmail")
        services = data.get("services", {})
        self.assertIn("gmail", services)
        self.assertTrue(bool(services["gmail"].get("authoritative")))
        self.assertIn("fallbackReason", services["gmail"])
        self.assertIn("lastError", services["gmail"])
        self.assertIn("sampleCount", services["gmail"])
        self.assertIn("requiredScopes", services["gmail"])
        self.assertIn("riskLevels", services["gmail"])
        self.assertIn("domain", services["gmail"])
        self.assertIn("providerId", services["gmail"])
        self.assertIn("grants", data)
        self.assertIn("grantsSummary", data)

    def test_connector_service_diagnostics_are_shell_ready(self) -> None:
        payload = main.build_connector_service_diagnostics("gmail")

        self.assertTrue(bool(payload.get("ok")))
        data = payload.get("data", {})
        self.assertEqual(data.get("service"), "gmail")
        self.assertIn("serviceInfo", data)
        self.assertIn("snapshot", data)
        self.assertIn("relevantGrants", data)
        self.assertIn("actions", data)
        self.assertIn("sampleItems", data.get("snapshot", {}))

    def test_open_shell_object_repo_updates_workspace_focus(self) -> None:
        item = main.content_commit("document", "Shell Repo", "<p>repo object</p>", message="Seed")
        session = main.ensure_session("sess-shell-object")

        payload = main.open_shell_object(session, {"kind": "repo", "domain": "document", "name": "Shell Repo", "branch": "main"})

        self.assertTrue(bool(payload.get("ok")))
        target = payload.get("data", {}).get("target", {})
        self.assertEqual(target.get("type"), "repo")
        self.assertEqual(target.get("name"), "Shell Repo")
        active = session.workspace.get("activeContent", {}) if isinstance(session.workspace, dict) else {}
        self.assertEqual(active.get("itemId"), item["itemId"])
        self.assertEqual(active.get("name"), "Shell Repo")

    def test_open_shell_object_service_maps_to_scene(self) -> None:
        session = main.ensure_session("sess-shell-service")

        payload = main.open_shell_object(session, {"kind": "service", "service": "gmail"})

        self.assertTrue(bool(payload.get("ok")))
        target = payload.get("data", {}).get("target", {})
        self.assertEqual(target.get("type"), "scene")
        self.assertEqual(target.get("scene"), "email")
        self.assertEqual(target.get("service"), "gmail")

    def test_workspace_file_ops_return_truthful_storage_contract(self) -> None:
        session = main.ensure_session("sess-files")

        listed = main.run_operation(session, {"type": "list_files", "payload": {"path": "."}})

        self.assertTrue(bool(listed.get("ok")))
        data = listed.get("data", {})
        self.assertEqual(data.get("storage"), "workspace")
        self.assertEqual(data.get("source"), "live")
        self.assertTrue(bool(data.get("authoritative")))

    def test_connector_grant_ops_return_grant_report_data(self) -> None:
        session = main.ensure_session("sess-grants")

        granted = main.run_operation(session, {"type": "grant_connector_scope", "payload": {"scope": "web.page.read"}})
        self.assertTrue(bool(granted.get("ok")))
        self.assertIn("data", granted)
        self.assertIn("items", granted.get("data", {}))
        first = (granted.get("data", {}).get("items", []) or [{}])[0]
        self.assertIn("domains", first)
        self.assertIn("providers", first)
        self.assertIn("risk", first)
        self.assertIn("support", first)

        listed = main.run_operation(session, {"type": "list_connector_grants", "payload": {}})
        self.assertTrue(bool(listed.get("ok")))
        self.assertIn("data", listed)
        self.assertIn("items", listed.get("data", {}))

    def test_runtime_profile_and_self_check_payloads_exist(self) -> None:
        session = main.ensure_session("sess-runtime")
        session.turn_history = [
            {"ok": True, "performance": {"totalMs": 120, "withinBudget": True}},
            {"ok": False, "performance": {"totalMs": 980, "withinBudget": False}},
        ]

        profile = main.build_runtime_profile_payload(session, "sess-runtime", limit=50)
        self_check = main.build_runtime_self_check_report(session, "sess-runtime")

        self.assertTrue(bool(profile.get("ok")))
        self.assertIn("latencyMs", profile)
        self.assertIn("outcomes", profile)
        self.assertTrue(bool(self_check.get("ok")))
        self.assertIn("checks", self_check)
        health = main.get_runtime_health_payload(session, "sess-runtime")
        self.assertIn("workspace", health)
        self.assertIn("worktreeCount", health.get("workspace", {}))

    def test_handoff_and_continuity_payloads_exist(self) -> None:
        session = main.ensure_session("sess-cont")
        session.handoff = {
            "activeDeviceId": "desktop-1",
            "pending": None,
            "lastClaimAt": 123,
            "stats": {"starts": 1, "claims": 1, "budgetMs": 800},
        }

        handoff = main.build_handoff_stats_payload(session, "sess-cont")
        continuity = main.build_continuity_payload(session, "sess-cont")

        self.assertTrue(bool(handoff.get("ok")))
        self.assertIn("stats", handoff)
        self.assertTrue(bool(continuity.get("ok")))
        self.assertIn("health", continuity)
        self.assertIn("workspace", continuity)
        self.assertIn("notifications", continuity)

    def test_slo_update_emits_new_alert_payload(self) -> None:
        session = main.ensure_session("sess-slo")
        session.slo["breachStreak"] = int(main.SLO_BREACH_STREAK_FOR_THROTTLE) - 1

        slo = main.update_slo_state(session, {"totalMs": 2000, "budgetMs": 800})

        self.assertTrue(bool(slo.get("throttled")))
        self.assertIn("newAlert", slo)
        self.assertIsInstance(slo.get("newAlert"), dict)

    def test_runtime_services_payload_exists(self) -> None:
        payload = main.build_runtime_services_payload()

        self.assertTrue(bool(payload.get("ok")))
        services = payload.get("services", {})
        self.assertIn("backend", services)
        self.assertIn("nous_embedded", services)
        self.assertIn("nous_gateway", services)

    def test_continuity_next_actions_payload_exists(self) -> None:
        session = main.ensure_session("sess-diagnostics")
        payload = main.build_continuity_next_actions(session, "sess-diagnostics", limit=5)

        self.assertTrue(bool(payload.get("ok")))
        self.assertIn("summary", payload)
        self.assertIn("items", payload)

    def test_continuity_autopilot_mode_recommendation_exists(self) -> None:
        session = main.ensure_session("sess-autopilot")
        payload = main.build_continuity_autopilot_mode_recommendation(session, "sess-autopilot")

        self.assertTrue(bool(payload.get("ok")))
        self.assertIn("recommendedMode", payload)

    def test_session_diagnostics_exposes_control_plane_reports(self) -> None:
        session = main.ensure_session("sess-diag-control")
        session.continuity_autopilot["enabled"] = True
        session.continuity_autopilot["autoAlignMode"] = True
        session.continuity_autopilot["mode"] = "normal"
        main.append_continuity_history_snapshot(session, "sess-diag-control", "test")
        main.append_continuity_autopilot_posture_snapshot(session, "sess-diag-control", "test")

        payload = asyncio.run(main.get_session_diagnostics("sess-diag-control"))

        self.assertTrue(bool(payload.get("ok")))
        self.assertIn("continuityAutopilotPreview", payload)
        self.assertIn("continuityAutopilotGuardrails", payload)
        self.assertIn("continuityAutopilotMode", payload)
        self.assertIn("continuityAutopilotDrift", payload)
        self.assertIn("continuityAutopilotAlignment", payload)
        self.assertIn("continuityAutopilotPolicyMatrix", payload)
        self.assertIn("continuityAutopilotPosture", payload)
        self.assertIn("continuityAutopilotPostureHistory", payload)
        self.assertIn("continuityAutopilotPostureAnomalies", payload)
        self.assertIn("continuityAutopilotPostureActions", payload)
        self.assertIn("continuityAutopilotPostureActionMetrics", payload)
        self.assertIn("continuityAutopilotPosturePolicyMatrix", payload)
