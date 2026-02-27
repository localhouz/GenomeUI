from __future__ import annotations

import asyncio
import copy
import os
import unittest
from unittest import mock

import backend.main as main


class ConnectorUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self._vault_before = copy.deepcopy(main.CONNECTOR_VAULT)

    def tearDown(self) -> None:
        main.CONNECTOR_VAULT = self._vault_before

    def test_connector_summary_alias_fields(self) -> None:
        manifests = main.list_connector_manifests()
        summary = main.connector_summary(manifests)
        self.assertGreaterEqual(int(summary.get("count", 0)), 1)
        self.assertIn("domains", summary)
        self.assertIn("scopes", summary)
        self.assertIn("mobileFull", summary)

    def test_connector_contracts_shape(self) -> None:
        contracts = main.CONNECTOR_ADAPTER_CONTRACTS
        providers = contracts.get("providers", {}) if isinstance(contracts, dict) else {}
        self.assertIn("weather", providers)
        self.assertIn("banking", providers)
        self.assertIn("social", providers)
        self.assertIn("telephony", providers)

    def test_connector_device_counts(self) -> None:
        manifests = main.list_connector_manifests()
        mobile = main.connector_device_counts(manifests, "mobile")
        self.assertIsInstance(mobile, dict)
        self.assertGreaterEqual(int(mobile.get("full", 0)), 1)

    def test_weather_fallback_is_deterministic(self) -> None:
        a = main.weather_fallback_snapshot("Austin")
        b = main.weather_fallback_snapshot("Austin")
        self.assertEqual(a["temperatureF"], b["temperatureF"])
        self.assertEqual(a["condition"], b["condition"])
        self.assertEqual(a["source"], "fallback")
        self.assertIn("hourly", a)
        self.assertIsInstance(a.get("hourly"), list)
        self.assertGreaterEqual(len(a.get("hourly", [])), 6)

    def test_weather_mock_snapshot_is_deterministic(self) -> None:
        a = main.weather_mock_snapshot("Seattle")
        b = main.weather_mock_snapshot("Seattle")
        self.assertEqual(a["temperatureF"], b["temperatureF"])
        self.assertEqual(a["condition"], b["condition"])
        self.assertEqual(a["source"], "mock")
        self.assertIn("hourly", a)
        self.assertIsInstance(a.get("hourly"), list)

    def test_weather_live_mode_degrades_to_fallback_on_provider_error(self) -> None:
        with mock.patch("backend.main.httpx.Client", side_effect=RuntimeError("offline")):
            snap = main.weather_read_snapshot("Tulsa, Oklahoma", provider_mode="live")
        self.assertTrue(bool(snap.get("ok", False)))
        self.assertEqual(str(snap.get("source", "")), "fallback")
        self.assertEqual(str(snap.get("degradedFrom", "")), "open-meteo")

    def test_web_fetch_snapshot_scaffold_is_deterministic(self) -> None:
        a = main.web_fetch_snapshot("https://example.com", provider_mode="scaffold")
        b = main.web_fetch_snapshot("https://example.com", provider_mode="scaffold")
        self.assertTrue(bool(a.get("ok", False)))
        self.assertEqual(str(a.get("source", "")), "scaffold")
        self.assertEqual(str(a.get("body", "")), str(b.get("body", "")))
        self.assertIn("thumbnail", a)

    def test_provider_mode_normalization(self) -> None:
        self.assertEqual(main.normalize_connector_provider_mode("mock"), "mock")
        self.assertEqual(main.normalize_connector_provider_mode("live"), "live")
        self.assertEqual(main.normalize_connector_provider_mode("bad-value"), "auto")

    def test_service_mode_normalization(self) -> None:
        self.assertEqual(main.normalize_connector_service_mode("mock"), "mock")
        self.assertEqual(main.normalize_connector_service_mode("live"), "live")
        self.assertEqual(main.normalize_connector_service_mode("weird"), "scaffold")

    def test_provider_client_reports_not_configured(self) -> None:
        ok, payload, error_code = main.provider_client_json_request("GET", "", "/health", "")
        self.assertFalse(ok)
        self.assertEqual(payload, {})
        self.assertEqual(error_code, "not_configured")

    def test_provider_adapter_settings_prefers_vault_token(self) -> None:
        main.CONNECTOR_VAULT = main.default_connector_vault_state()
        main.CONNECTOR_VAULT["secrets"] = {"social.api.token": "vault-token"}
        before = os.environ.get("SOCIAL_API_BASE_URL")
        try:
            os.environ["SOCIAL_API_BASE_URL"] = "https://social.example.com/"
            base_url, token = main.provider_adapter_settings("SOCIAL_API_BASE_URL", "SOCIAL_API_TOKEN", "social.api.token")
        finally:
            if before is None:
                os.environ.pop("SOCIAL_API_BASE_URL", None)
            else:
                os.environ["SOCIAL_API_BASE_URL"] = before
        self.assertEqual(base_url, "https://social.example.com")
        self.assertEqual(token, "vault-token")

    def test_policy_allows_weather_without_scope(self) -> None:
        main.CONNECTOR_VAULT = main.default_connector_vault_state()
        op = {"type": "weather_forecast", "payload": {"location": "Austin"}}
        capability = main.resolve_capability(op)
        policy = main.evaluate_policy(op, capability)
        self.assertTrue(bool(policy.get("allowed", False)))
        self.assertEqual(str(policy.get("code", "")), "ok")

    def test_policy_allows_weather_with_scope(self) -> None:
        main.CONNECTOR_VAULT = main.default_connector_vault_state()
        main.CONNECTOR_VAULT["grants"] = {
            "weather.forecast.read": {"granted": True, "grantedAt": 1, "expiresAt": 0}
        }
        op = {"type": "weather_forecast", "payload": {"location": "Austin"}}
        capability = main.resolve_capability(op)
        policy = main.evaluate_policy(op, capability)
        self.assertTrue(bool(policy.get("allowed", False)))
        self.assertEqual(str(policy.get("code", "")), "ok")

    def test_web_fetch_policy_requires_scope(self) -> None:
        main.CONNECTOR_VAULT = main.default_connector_vault_state()
        op = {"type": "fetch_url", "payload": {"url": "https://example.com"}}
        capability = main.resolve_capability(op)
        policy = main.evaluate_policy(op, capability)
        self.assertFalse(bool(policy.get("allowed", True)))
        self.assertEqual(str(policy.get("code", "")), "connector_scope_required")

    def test_web_fetch_policy_allows_with_scope(self) -> None:
        main.CONNECTOR_VAULT = main.default_connector_vault_state()
        main.CONNECTOR_VAULT["grants"] = {
            "web.page.read": {"granted": True, "grantedAt": 1, "expiresAt": 0}
        }
        op = {"type": "fetch_url", "payload": {"url": "https://example.com"}}
        capability = main.resolve_capability(op)
        policy = main.evaluate_policy(op, capability)
        self.assertTrue(bool(policy.get("allowed", False)))
        self.assertEqual(str(policy.get("code", "")), "ok")

    def test_web_summarize_policy_requires_scope(self) -> None:
        main.CONNECTOR_VAULT = main.default_connector_vault_state()
        op = {"type": "web_summarize", "payload": {"url": "https://example.com"}}
        capability = main.resolve_capability(op)
        policy = main.evaluate_policy(op, capability)
        self.assertFalse(bool(policy.get("allowed", True)))
        self.assertEqual(str(policy.get("code", "")), "connector_scope_required")

    def test_web_summarize_policy_allows_with_scope(self) -> None:
        main.CONNECTOR_VAULT = main.default_connector_vault_state()
        main.CONNECTOR_VAULT["grants"] = {
            "web.page.read": {"granted": True, "grantedAt": 1, "expiresAt": 0}
        }
        op = {"type": "web_summarize", "payload": {"url": "https://example.com"}}
        capability = main.resolve_capability(op)
        policy = main.evaluate_policy(op, capability)
        self.assertTrue(bool(policy.get("allowed", False)))
        self.assertEqual(str(policy.get("code", "")), "ok")

    def test_web_search_policy_requires_scope(self) -> None:
        main.CONNECTOR_VAULT = main.default_connector_vault_state()
        op = {"type": "web_search", "payload": {"query": "genome ui"}}
        capability = main.resolve_capability(op)
        policy = main.evaluate_policy(op, capability)
        self.assertFalse(bool(policy.get("allowed", True)))
        self.assertEqual(str(policy.get("code", "")), "connector_scope_required")

    def test_web_search_policy_allows_with_scope(self) -> None:
        main.CONNECTOR_VAULT = main.default_connector_vault_state()
        main.CONNECTOR_VAULT["grants"] = {
            "web.page.read": {"granted": True, "grantedAt": 1, "expiresAt": 0}
        }
        op = {"type": "web_search", "payload": {"query": "genome ui"}}
        capability = main.resolve_capability(op)
        policy = main.evaluate_policy(op, capability)
        self.assertTrue(bool(policy.get("allowed", False)))
        self.assertEqual(str(policy.get("code", "")), "ok")

    def test_web_search_snapshot_returns_items(self) -> None:
        snap = main.web_search_snapshot("genome ui")
        self.assertTrue(bool(snap.get("ok", False)))
        items = snap.get("items", []) if isinstance(snap.get("items"), list) else []
        self.assertGreaterEqual(len(items), 1)
        self.assertIn("https://", str(items[0].get("url", "")))
        self.assertTrue(bool(str(items[0].get("host", "")).strip()))
        self.assertIn("favicon", items[0])

    def test_scheduler_emits_background_reminder_event(self) -> None:
        sid = f"unit-reminder-{os.getpid()}-{id(self)}"
        session = main.ensure_session(sid)
        session.jobs = []
        schedule = main.run_operation(
            session,
            {"type": "schedule_remind_once", "payload": {"text": "stretch", "delayMs": 1000}},
        )
        self.assertTrue(bool(schedule.get("ok", False)))
        queue: asyncio.Queue = asyncio.Queue()
        session.subscribers.add(queue)
        try:
            asyncio.run(main.run_due_jobs_for_session(sid, session, force=True))
            payload = asyncio.run(queue.get())
            self.assertIsInstance(payload, dict)
            events = payload.get("backgroundEvents", []) if isinstance(payload.get("backgroundEvents"), list) else []
            self.assertGreaterEqual(len(events), 1)
            self.assertEqual(str(events[0].get("type", "")), "reminder_fired")
        finally:
            session.subscribers.discard(queue)
            main.SESSIONS.pop(sid, None)

    def test_continuity_alert_event_emits_and_respects_cooldown(self) -> None:
        sid = f"unit-cont-alert-{os.getpid()}-{id(self)}"
        session = main.ensure_session(sid)
        session.continuity_history = [
            {
                "ts": 1,
                "source": "seed",
                "status": "healthy",
                "score": 92,
                "activeDevices": 1,
                "presenceTotal": 1,
                "staleDevices": 0,
                "handoffBreaches": 0,
                "handoffP95Ms": 120,
                "handoffBudgetMs": 500,
            },
            {
                "ts": 2,
                "source": "seed",
                "status": "critical",
                "score": 48,
                "activeDevices": 0,
                "presenceTotal": 1,
                "staleDevices": 1,
                "handoffBreaches": 2,
                "handoffP95Ms": 780,
                "handoffBudgetMs": 500,
            },
        ]
        session.presence = main.ensure_presence_state(
            {
                "devices": {
                    "dev-a": {
                        "deviceId": "dev-a",
                        "label": "Desktop",
                        "platform": "desktop",
                        "userAgent": "unit",
                        "lastSeenAt": 1,
                    }
                },
                "updatedAt": 1,
            }
        )

        first = main.maybe_continuity_alert_event(session, sid)
        self.assertIsInstance(first, dict)
        self.assertEqual(str(first.get("type", "")), "continuity_alert")
        self.assertGreater(int(first.get("anomalyCount", 0) or 0), 0)
        self.assertGreater(int(session.last_continuity_alert_at or 0), 0)

        second = main.maybe_continuity_alert_event(session, sid)
        self.assertIsNone(second)
        main.SESSIONS.pop(sid, None)

    def test_web_fetch_policy_still_blocks_localhost(self) -> None:
        main.CONNECTOR_VAULT = main.default_connector_vault_state()
        main.CONNECTOR_VAULT["grants"] = {
            "web.page.read": {"granted": True, "grantedAt": 1, "expiresAt": 0}
        }
        op = {"type": "fetch_url", "payload": {"url": "http://localhost:8787"}}
        capability = main.resolve_capability(op)
        policy = main.evaluate_policy(op, capability)
        self.assertFalse(bool(policy.get("allowed", True)))
        self.assertEqual(str(policy.get("code", "")), "url_not_allowed")

    def test_contacts_lookup_policy_requires_scope(self) -> None:
        main.CONNECTOR_VAULT = main.default_connector_vault_state()
        op = {"type": "contacts_lookup", "payload": {"query": "mike"}}
        capability = main.resolve_capability(op)
        policy = main.evaluate_policy(op, capability)
        self.assertFalse(bool(policy.get("allowed", True)))
        self.assertEqual(str(policy.get("code", "")), "connector_scope_required")

    def test_contacts_lookup_policy_allows_with_scope(self) -> None:
        main.CONNECTOR_VAULT = main.default_connector_vault_state()
        main.CONNECTOR_VAULT["grants"] = {
            "contacts.read": {"granted": True, "grantedAt": 1, "expiresAt": 0}
        }
        op = {"type": "contacts_lookup", "payload": {"query": "mike"}}
        capability = main.resolve_capability(op)
        policy = main.evaluate_policy(op, capability)
        self.assertTrue(bool(policy.get("allowed", False)))
        self.assertEqual(str(policy.get("code", "")), "ok")

    def test_contacts_snapshot_filters_query(self) -> None:
        snap = main.contacts_lookup_snapshot("mike")
        self.assertTrue(bool(snap.get("ok", False)))
        items = snap.get("items", []) if isinstance(snap.get("items"), list) else []
        self.assertGreaterEqual(len(items), 1)
        self.assertIn("mike", str(items[0].get("name", "")).lower())

    def test_intent_parses_one_shot_reminder_command(self) -> None:
        envelope = main.compile_intent_envelope("remind me to drink water in 10m")
        writes = (envelope.get("stateIntent", {}) or {}).get("writeOperations", [])
        types = [str(item.get("type", "")) for item in (writes if isinstance(writes, list) else [])]
        self.assertIn("schedule_remind_once", types)

    def test_intent_parses_show_and_cancel_reminders(self) -> None:
        show_env = main.compile_intent_envelope("show reminders")
        show_ops = (show_env.get("stateIntent", {}) or {}).get("writeOperations", [])
        show_types = [str(item.get("type", "")) for item in (show_ops if isinstance(show_ops, list) else [])]
        self.assertIn("list_reminders", show_types)
        cancel_env = main.compile_intent_envelope("cancel reminder 1")
        cancel_ops = (cancel_env.get("stateIntent", {}) or {}).get("writeOperations", [])
        cancel_types = [str(item.get("type", "")) for item in (cancel_ops if isinstance(cancel_ops, list) else [])]
        self.assertIn("cancel_reminder", cancel_types)

    def test_intent_parses_quoted_weather_prompt(self) -> None:
        envelope = main.compile_intent_envelope('"show weather in Tulsa, Oklahoma"')
        writes = (envelope.get("stateIntent", {}) or {}).get("writeOperations", [])
        self.assertGreaterEqual(len(writes), 1)
        op = writes[0] if isinstance(writes[0], dict) else {}
        self.assertEqual(str(op.get("type", "")), "weather_forecast")
        payload = op.get("payload", {}) if isinstance(op.get("payload"), dict) else {}
        self.assertEqual(str(payload.get("location", "")), "Tulsa, Oklahoma")

    def test_intent_parses_natural_weather_here_phrase(self) -> None:
        envelope = main.compile_intent_envelope("what's the weather where i am")
        writes = (envelope.get("stateIntent", {}) or {}).get("writeOperations", [])
        self.assertGreaterEqual(len(writes), 1)
        op = writes[0] if isinstance(writes[0], dict) else {}
        self.assertEqual(str(op.get("type", "")), "weather_forecast")
        payload = op.get("payload", {}) if isinstance(op.get("payload"), dict) else {}
        self.assertEqual(str(payload.get("location", "")), "__current__")

    def test_intent_parses_show_weather_without_city_as_current(self) -> None:
        envelope = main.compile_intent_envelope("show weather")
        writes = (envelope.get("stateIntent", {}) or {}).get("writeOperations", [])
        self.assertGreaterEqual(len(writes), 1)
        op = writes[0] if isinstance(writes[0], dict) else {}
        self.assertEqual(str(op.get("type", "")), "weather_forecast")
        payload = op.get("payload", {}) if isinstance(op.get("payload"), dict) else {}
        self.assertEqual(str(payload.get("location", "")), "__current__")

    def test_resolve_user_location_hint_uses_neutral_non_hardcoded_fallback(self) -> None:
        prev_home = os.environ.pop("GENOME_HOME_LOCATION", None)
        prev_default = os.environ.pop("GENOME_DEFAULT_LOCATION", None)
        prev_secret = main.connector_secret_get("user.home.location")
        try:
            if isinstance(main.CONNECTOR_VAULT, dict):
                main.CONNECTOR_VAULT.pop("user.home.location", None)
            hint = str(main.resolve_user_location_hint() or "").strip()
            self.assertNotEqual(hint.lower(), "new york")
            self.assertNotEqual(hint.lower(), "new york, us")
            self.assertGreaterEqual(len(hint), 3)
        finally:
            if prev_home is not None:
                os.environ["GENOME_HOME_LOCATION"] = prev_home
            if prev_default is not None:
                os.environ["GENOME_DEFAULT_LOCATION"] = prev_default
            if prev_secret:
                main.connector_secret_set("user.home.location", prev_secret)

    def test_intent_parses_where_am_i(self) -> None:
        envelope = main.compile_intent_envelope("where am i")
        writes = (envelope.get("stateIntent", {}) or {}).get("writeOperations", [])
        self.assertGreaterEqual(len(writes), 1)
        op = writes[0] if isinstance(writes[0], dict) else {}
        self.assertEqual(str(op.get("type", "")), "location_status")

    def test_intent_parses_shopping_semantic_query(self) -> None:
        envelope = main.compile_intent_envelope("show me new running shoes")
        writes = (envelope.get("stateIntent", {}) or {}).get("writeOperations", [])
        self.assertGreaterEqual(len(writes), 1)
        op = writes[0] if isinstance(writes[0], dict) else {}
        self.assertEqual(str(op.get("type", "")), "shop_catalog_search")
        payload = op.get("payload", {}) if isinstance(op.get("payload"), dict) else {}
        self.assertEqual(str(payload.get("category", "")), "shoes")

    def test_shopping_catalog_snapshot_returns_image_items(self) -> None:
        snap = main.shopping_catalog_snapshot("running shoes", category="shoes")
        self.assertTrue(bool(snap.get("ok", False)))
        items = snap.get("items", []) if isinstance(snap.get("items"), list) else []
        self.assertGreaterEqual(len(items), 1)
        first = items[0] if isinstance(items[0], dict) else {}
        self.assertIn("http", str(first.get("imageUrl", "")))

    def test_intent_parses_brand_only_puma_query(self) -> None:
        envelope = main.compile_intent_envelope("show me size 8 1/2 pumas, for men")
        writes = (envelope.get("stateIntent", {}) or {}).get("writeOperations", [])
        self.assertGreaterEqual(len(writes), 1)
        op = writes[0] if isinstance(writes[0], dict) else {}
        self.assertEqual(str(op.get("type", "")), "shop_catalog_search")
        payload = op.get("payload", {}) if isinstance(op.get("payload"), dict) else {}
        self.assertEqual(str(payload.get("category", "")), "shoes")

    def test_shopping_catalog_matches_puma_brand(self) -> None:
        snap = main.shopping_catalog_snapshot("show me size 8 1/2 pumas, for men.", category="shoes")
        items = snap.get("items", []) if isinstance(snap.get("items"), list) else []
        self.assertGreaterEqual(len(items), 6)
        brands = [str(item.get("brand", "")).lower() for item in items if isinstance(item, dict)]
        self.assertIn("puma", brands)
        self.assertEqual(brands[0], "puma")
        self.assertTrue(all("puma.com" in str(item.get("sourceHost", "")).lower() for item in items if isinstance(item, dict)))
        target = snap.get("sourceTarget", {}) if isinstance(snap.get("sourceTarget"), dict) else {}
        self.assertIn("puma.com", str(target.get("url", "")).lower())
        self.assertEqual(str(target.get("mode", "")), "direct")
        self.assertIn("search?q=", str(target.get("url", "")).lower())
        pause_env = main.compile_intent_envelope("pause reminder 1")
        pause_ops = (pause_env.get("stateIntent", {}) or {}).get("writeOperations", [])
        pause_types = [str(item.get("type", "")) for item in (pause_ops if isinstance(pause_ops, list) else [])]
        self.assertIn("pause_reminder", pause_types)
        resume_env = main.compile_intent_envelope("resume reminder 1")
        resume_ops = (resume_env.get("stateIntent", {}) or {}).get("writeOperations", [])
        resume_types = [str(item.get("type", "")) for item in (resume_ops if isinstance(resume_ops, list) else [])]
        self.assertIn("resume_reminder", resume_types)
        web_status_env = main.compile_intent_envelope("show web status")
        web_status_ops = (web_status_env.get("stateIntent", {}) or {}).get("writeOperations", [])
        web_status_types = [str(item.get("type", "")) for item in (web_status_ops if isinstance(web_status_ops, list) else [])]
        self.assertIn("web_status", web_status_types)
        contacts_status_env = main.compile_intent_envelope("show contacts status")
        contacts_status_ops = (contacts_status_env.get("stateIntent", {}) or {}).get("writeOperations", [])
        contacts_status_types = [str(item.get("type", "")) for item in (contacts_status_ops if isinstance(contacts_status_ops, list) else [])]
        self.assertIn("contacts_status", contacts_status_types)
        reminder_status_env = main.compile_intent_envelope("show reminder status")
        reminder_status_ops = (reminder_status_env.get("stateIntent", {}) or {}).get("writeOperations", [])
        reminder_status_types = [str(item.get("type", "")) for item in (reminder_status_ops if isinstance(reminder_status_ops, list) else [])]
        self.assertIn("reminder_status", reminder_status_types)

    def test_shopping_catalog_matches_jordan_brand_and_direct_site(self) -> None:
        snap = main.shopping_catalog_snapshot("show me men's jordan size 8.5", category="shoes")
        self.assertTrue(bool(snap.get("ok", False)))
        items = snap.get("items", []) if isinstance(snap.get("items"), list) else []
        self.assertGreaterEqual(len(items), 2)
        brands = [str(item.get("brand", "")).lower() for item in items if isinstance(item, dict)]
        self.assertIn("jordan", brands)
        self.assertTrue(all("nike.com" in str(item.get("sourceHost", "")).lower() for item in items if isinstance(item, dict)))
        target = snap.get("sourceTarget", {}) if isinstance(snap.get("sourceTarget"), dict) else {}
        self.assertIn("nike.com", str(target.get("url", "")).lower())
        self.assertIn("nike.com", str(target.get("host", "")).lower())
        self.assertEqual(str(target.get("mode", "")), "direct")
        self.assertIn("?q=", str(target.get("url", "")).lower())

    def test_shopping_catalog_prefers_first_explicit_brand_in_query(self) -> None:
        snap = main.shopping_catalog_snapshot("show me jordan then puma shoes for men size 8.5", category="shoes")
        self.assertTrue(bool(snap.get("ok", False)))
        target = snap.get("sourceTarget", {}) if isinstance(snap.get("sourceTarget"), dict) else {}
        self.assertIn("nike.com", str(target.get("url", "")).lower())

    def test_shopping_catalog_normalizes_nb_alias_to_new_balance(self) -> None:
        snap = main.shopping_catalog_snapshot("show me nb shoes size 8.5 for men", category="shoes")
        self.assertTrue(bool(snap.get("ok", False)))
        items = snap.get("items", []) if isinstance(snap.get("items"), list) else []
        self.assertGreaterEqual(len(items), 1)
        brands = [str(item.get("brand", "")).lower() for item in items if isinstance(item, dict)]
        self.assertIn("new balance", brands)
        target = snap.get("sourceTarget", {}) if isinstance(snap.get("sourceTarget"), dict) else {}
        self.assertIn("newbalance.com", str(target.get("url", "")).lower())
        self.assertIn("newbalance.com", str(target.get("host", "")).lower())

    def test_shopping_catalog_nike_direct_returns_richer_result_set(self) -> None:
        snap = main.shopping_catalog_snapshot("show me nike running shoes for men size 8.5", category="shoes")
        self.assertTrue(bool(snap.get("ok", False)))
        items = snap.get("items", []) if isinstance(snap.get("items"), list) else []
        self.assertGreaterEqual(len(items), 3)
        self.assertTrue(all("nike.com" in str(item.get("sourceHost", "")).lower() for item in items if isinstance(item, dict)))
        target = snap.get("sourceTarget", {}) if isinstance(snap.get("sourceTarget"), dict) else {}
        self.assertEqual(str(target.get("mode", "")), "direct")
        self.assertIn("nike.com", str(target.get("url", "")).lower())

    def test_shopping_catalog_direct_brand_merges_live_web_hits(self) -> None:
        original = main.web_search_snapshot
        try:
            def fake_snapshot(query: str) -> dict:
                self.assertIn("site:nike.com", str(query).lower())
                return {
                    "ok": True,
                    "source": "duckduckgo",
                    "query": query,
                    "items": [
                        {
                            "title": "Nike Pegasus 41 Road Running Shoes",
                            "url": "https://www.nike.com/t/pegasus-41-road-running-shoes-abc123",
                            "snippet": "Responsive ride for daily miles.",
                            "host": "nike.com",
                            "favicon": "https://www.nike.com/favicon.ico",
                            "thumbnail": "https://static.nike.com/a/images/pegasus-41.jpg",
                        },
                        {
                            "title": "Off host result",
                            "url": "https://example.com/off-host",
                            "snippet": "should be filtered by host",
                            "host": "example.com",
                            "favicon": "https://example.com/favicon.ico",
                            "thumbnail": "https://example.com/thumb.jpg",
                        },
                    ],
                }

            main.web_search_snapshot = fake_snapshot
            snap = main.shopping_catalog_snapshot("show me nike running shoes for men size 8.5", category="shoes")
            self.assertTrue(bool(snap.get("ok", False)))
            self.assertEqual(str(snap.get("source", "")), "brand-live")
            items = snap.get("items", []) if isinstance(snap.get("items"), list) else []
            self.assertGreaterEqual(len(items), 1)
            urls = [str(item.get("url", "")) for item in items if isinstance(item, dict)]
            self.assertTrue(any("pegasus-41-road-running-shoes" in url for url in urls))
            self.assertTrue(all("nike.com" in str(item.get("sourceHost", "")).lower() for item in items if isinstance(item, dict)))
        finally:
            main.web_search_snapshot = original

    def test_shopping_catalog_direct_brand_backfills_images_when_live_has_no_thumbnails(self) -> None:
        original = main.web_search_snapshot
        try:
            def fake_snapshot(query: str) -> dict:
                return {
                    "ok": True,
                    "source": "duckduckgo",
                    "query": query,
                    "items": [
                        {
                            "title": "Nike Vomero Product Page",
                            "url": "https://www.nike.com/t/vomero-running-shoes-xyz987",
                            "snippet": "Comfortable trainer.",
                            "host": "nike.com",
                            "favicon": "",
                            "thumbnail": "",
                        }
                    ],
                }

            main.web_search_snapshot = fake_snapshot
            snap = main.shopping_catalog_snapshot("show me nike running shoes for men size 8.5", category="shoes")
            self.assertTrue(bool(snap.get("ok", False)))
            items = snap.get("items", []) if isinstance(snap.get("items"), list) else []
            self.assertGreaterEqual(len(items), 1)
            first = items[0] if isinstance(items[0], dict) else {}
            image_url = str(first.get("imageUrl", "")).strip().lower()
            self.assertTrue(image_url.startswith("http"))
            self.assertNotEqual(image_url, "")
        finally:
            main.web_search_snapshot = original

    def test_web_search_phrase_source_route_instagram(self) -> None:
        session = main.ensure_session("ut_web_phrase_ig")
        result = main.run_operation(session, {"type": "web_search", "payload": {"query": "show me running tips on instagram"}})
        self.assertTrue(bool(result.get("ok", False)))
        data = result.get("data", {}) if isinstance(result.get("data"), dict) else {}
        target = data.get("sourceTarget", {}) if isinstance(data.get("sourceTarget"), dict) else {}
        self.assertEqual(str(target.get("mode", "")), "direct")
        self.assertIn("instagram.com", str(target.get("url", "")).lower())

    def test_web_search_phrase_source_route_youtube(self) -> None:
        session = main.ensure_session("ut_web_phrase_yt")
        result = main.run_operation(session, {"type": "web_search", "payload": {"query": "best trail shoes on youtube"}})
        self.assertTrue(bool(result.get("ok", False)))
        data = result.get("data", {}) if isinstance(result.get("data"), dict) else {}
        target = data.get("sourceTarget", {}) if isinstance(data.get("sourceTarget"), dict) else {}
        self.assertEqual(str(target.get("mode", "")), "direct")
        self.assertIn("youtube.com", str(target.get("url", "")).lower())

    def test_web_search_run_operation_caps_payload_to_12_items(self) -> None:
        original = main.web_search_snapshot
        try:
            def fake_snapshot(query: str) -> dict:
                items = []
                for i in range(20):
                    items.append(
                        {
                            "title": f"Result {i + 1}",
                            "url": f"https://example.com/r/{i + 1}",
                            "snippet": "snippet",
                            "host": "example.com",
                            "favicon": "https://example.com/favicon.ico",
                            "thumbnail": "",
                        }
                    )
                return {"ok": True, "source": "mock", "query": query, "items": items}

            main.web_search_snapshot = fake_snapshot
            session = main.ensure_session("ut_web_cap_12")
            result = main.run_operation(session, {"type": "web_search", "payload": {"query": "cap test"}})
            self.assertTrue(bool(result.get("ok", False)))
            data = result.get("data", {}) if isinstance(result.get("data"), dict) else {}
            items = data.get("items", []) if isinstance(data.get("items"), list) else []
            self.assertEqual(len(items), 12)
        finally:
            main.web_search_snapshot = original

    def test_resolve_contact_target_from_name(self) -> None:
        target = main.resolve_contact_target("mike")
        self.assertIsNotNone(target)
        self.assertIn("mike", str((target or {}).get("name", "")).lower())
        self.assertTrue(main.looks_like_phone_target(str((target or {}).get("phone", ""))))

    def test_telephony_named_target_requires_contacts_scope(self) -> None:
        main.CONNECTOR_VAULT = main.default_connector_vault_state()
        main.CONNECTOR_VAULT["grants"] = {
            "telephony.call.start": {"granted": True, "grantedAt": 1, "expiresAt": 0}
        }
        op = {"type": "telephony_call_start", "payload": {"target": "mike", "confirmed": True}}
        capability = main.resolve_capability(op)
        policy = main.evaluate_policy(op, capability)
        self.assertFalse(bool(policy.get("allowed", True)))
        self.assertEqual(str(policy.get("code", "")), "connector_scope_required")

    def test_telephony_named_target_allows_with_contacts_scope(self) -> None:
        main.CONNECTOR_VAULT = main.default_connector_vault_state()
        main.CONNECTOR_VAULT["grants"] = {
            "telephony.call.start": {"granted": True, "grantedAt": 1, "expiresAt": 0},
            "contacts.read": {"granted": True, "grantedAt": 1, "expiresAt": 0},
        }
        op = {"type": "telephony_call_start", "payload": {"target": "mike", "confirmed": True}}
        capability = main.resolve_capability(op)
        policy = main.evaluate_policy(op, capability)
        self.assertTrue(bool(policy.get("allowed", False)))
        self.assertEqual(str(policy.get("code", "")), "ok")

    def test_telephony_policy_requires_scope(self) -> None:
        main.CONNECTOR_VAULT = main.default_connector_vault_state()
        op = {"type": "telephony_call_start", "payload": {"target": "5550100", "confirmed": True}}
        capability = main.resolve_capability(op)
        policy = main.evaluate_policy(op, capability)
        self.assertFalse(bool(policy.get("allowed", True)))
        self.assertEqual(str(policy.get("code", "")), "connector_scope_required")

    def test_telephony_policy_requires_confirmation(self) -> None:
        main.CONNECTOR_VAULT = main.default_connector_vault_state()
        main.CONNECTOR_VAULT["grants"] = {
            "telephony.call.start": {"granted": True, "grantedAt": 1, "expiresAt": 0}
        }
        op = {"type": "telephony_call_start", "payload": {"target": "5550100"}}
        capability = main.resolve_capability(op)
        policy = main.evaluate_policy(op, capability)
        self.assertFalse(bool(policy.get("allowed", True)))
        self.assertEqual(str(policy.get("code", "")), "confirmation_required")

    def test_telephony_policy_allows_with_scope_and_confirmation(self) -> None:
        main.CONNECTOR_VAULT = main.default_connector_vault_state()
        main.CONNECTOR_VAULT["grants"] = {
            "telephony.call.start": {"granted": True, "grantedAt": 1, "expiresAt": 0}
        }
        op = {"type": "telephony_call_start", "payload": {"target": "5550100", "confirmed": True}}
        capability = main.resolve_capability(op)
        policy = main.evaluate_policy(op, capability)
        self.assertTrue(bool(policy.get("allowed", False)))
        self.assertEqual(str(policy.get("code", "")), "ok")

    def test_banking_balance_policy_requires_scope(self) -> None:
        main.CONNECTOR_VAULT = main.default_connector_vault_state()
        op = {"type": "banking_balance_read", "payload": {}}
        capability = main.resolve_capability(op)
        policy = main.evaluate_policy(op, capability)
        self.assertFalse(bool(policy.get("allowed", True)))
        self.assertEqual(str(policy.get("code", "")), "connector_scope_required")

    def test_banking_balance_policy_allows_with_scope(self) -> None:
        main.CONNECTOR_VAULT = main.default_connector_vault_state()
        main.CONNECTOR_VAULT["grants"] = {
            "bank.account.balance.read": {"granted": True, "grantedAt": 1, "expiresAt": 0}
        }
        op = {"type": "banking_balance_read", "payload": {}}
        capability = main.resolve_capability(op)
        policy = main.evaluate_policy(op, capability)
        self.assertTrue(bool(policy.get("allowed", False)))
        self.assertEqual(str(policy.get("code", "")), "ok")

    def test_social_send_policy_requires_confirmation(self) -> None:
        main.CONNECTOR_VAULT = main.default_connector_vault_state()
        main.CONNECTOR_VAULT["grants"] = {
            "social.message.send": {"granted": True, "grantedAt": 1, "expiresAt": 0}
        }
        op = {"type": "social_message_send", "payload": {"text": "hello"}}
        capability = main.resolve_capability(op)
        policy = main.evaluate_policy(op, capability)
        self.assertFalse(bool(policy.get("allowed", True)))
        self.assertEqual(str(policy.get("code", "")), "confirmation_required")

    def test_social_send_policy_allows_confirmed(self) -> None:
        main.CONNECTOR_VAULT = main.default_connector_vault_state()
        main.CONNECTOR_VAULT["grants"] = {
            "social.message.send": {"granted": True, "grantedAt": 1, "expiresAt": 0}
        }
        op = {"type": "social_message_send", "payload": {"text": "hello", "confirmed": True}}
        capability = main.resolve_capability(op)
        policy = main.evaluate_policy(op, capability)
        self.assertTrue(bool(policy.get("allowed", False)))
        self.assertEqual(str(policy.get("code", "")), "ok")

    def test_social_send_policy_rejects_long_text(self) -> None:
        main.CONNECTOR_VAULT = main.default_connector_vault_state()
        main.CONNECTOR_VAULT["grants"] = {
            "social.message.send": {"granted": True, "grantedAt": 1, "expiresAt": 0}
        }
        text = "x" * 281
        op = {"type": "social_message_send", "payload": {"text": text, "confirmed": True}}
        capability = main.resolve_capability(op)
        policy = main.evaluate_policy(op, capability)
        self.assertFalse(bool(policy.get("allowed", True)))
        self.assertEqual(str(policy.get("code", "")), "invalid_payload")

    def test_banking_snapshot_live_reports_unavailable(self) -> None:
        snap = main.banking_balance_snapshot(provider_mode="live")
        self.assertFalse(bool(snap.get("ok", True)))
        self.assertEqual(str(snap.get("source", "")), "live")

    def test_social_snapshot_live_reports_unavailable(self) -> None:
        snap = main.social_feed_snapshot(provider_mode="live")
        self.assertFalse(bool(snap.get("ok", True)))
        self.assertEqual(str(snap.get("source", "")), "live")

    def test_connector_secret_status_shape(self) -> None:
        main.CONNECTOR_VAULT = main.default_connector_vault_state()
        main.CONNECTOR_VAULT["secrets"] = {"banking.api.token": "abc"}
        items = main.connector_secret_status(["banking.api.token", "social.api.token"])
        by_key = {str(item.get("key", "")): bool(item.get("configured", False)) for item in items}
        self.assertTrue(by_key.get("banking.api.token", False))
        self.assertFalse(by_key.get("social.api.token", True))

    def test_vault_encrypt_decrypt_roundtrip(self) -> None:
        payload = {
            "version": 1,
            "grants": {"weather.forecast.read": {"granted": True, "grantedAt": 1, "expiresAt": 0}},
            "secrets": {"weather.api.key": "x"},
            "updatedAt": 1,
        }
        blob = main.encrypt_connector_vault(payload)
        out = main.decrypt_connector_vault(blob)
        self.assertEqual(out["version"], 1)
        self.assertIn("weather.forecast.read", out.get("grants", {}))

    def test_social_feed_run_operation_includes_structured_data(self) -> None:
        session = main.ensure_session("unit_social_data")
        res = main.run_operation(session, {"type": "social_feed_read", "payload": {}})
        self.assertTrue(bool(res.get("ok", False)))
        data = res.get("data", {})
        self.assertIsInstance(data, dict)
        self.assertIsInstance(data.get("items", []), list)
        self.assertTrue(str(data.get("source", "")).strip())

    def test_banking_read_run_operation_includes_structured_data(self) -> None:
        session = main.ensure_session("unit_banking_data")
        bal = main.run_operation(session, {"type": "banking_balance_read", "payload": {}})
        self.assertTrue(bool(bal.get("ok", False)))
        bal_data = bal.get("data", {})
        self.assertIsInstance(bal_data, dict)
        self.assertIn("available", bal_data)
        self.assertIn("ledger", bal_data)
        self.assertTrue(str(bal_data.get("currency", "")).strip())

        tx = main.run_operation(session, {"type": "banking_transactions_read", "payload": {"limit": 3}})
        self.assertTrue(bool(tx.get("ok", False)))
        tx_data = tx.get("data", {})
        self.assertIsInstance(tx_data, dict)
        self.assertIsInstance(tx_data.get("items", []), list)

    def test_contacts_lookup_run_operation_includes_structured_data(self) -> None:
        session = main.ensure_session("unit_contacts_data")
        res = main.run_operation(session, {"type": "contacts_lookup", "payload": {"query": "mike"}})
        self.assertTrue(bool(res.get("ok", False)))
        data = res.get("data", {})
        self.assertIsInstance(data, dict)
        self.assertIsInstance(data.get("items", []), list)
        self.assertEqual(str(data.get("query", "")), "mike")

    def test_telephony_run_operation_includes_structured_data(self) -> None:
        session = main.ensure_session("unit_telephony_data")
        status = main.run_operation(session, {"type": "telephony_status", "payload": {}})
        self.assertTrue(bool(status.get("ok", False)))
        status_data = status.get("data", {})
        self.assertIsInstance(status_data, dict)
        self.assertEqual(str(status_data.get("mode", "")), "status")

        prepared = main.run_operation(
            session,
            {"type": "telephony_call_start", "payload": {"target": "5550100"}},
        )
        self.assertTrue(bool(prepared.get("ok", False)))
        prepared_data = prepared.get("data", {})
        self.assertIsInstance(prepared_data, dict)
        self.assertEqual(str(prepared_data.get("target", "")), "5550100")

    def test_file_ops_include_structured_data(self) -> None:
        session = main.ensure_session("unit_files_data")
        listed = main.run_operation(session, {"type": "list_files", "payload": {"path": "."}})
        self.assertTrue(bool(listed.get("ok", False)))
        listed_data = listed.get("data", {})
        self.assertIsInstance(listed_data, dict)
        self.assertIsInstance(listed_data.get("items", []), list)
        self.assertIsInstance(str(listed_data.get("path", "")), str)

        read = main.run_operation(session, {"type": "read_file", "payload": {"path": "README.md"}})
        self.assertTrue(bool(read.get("ok", False)))
        read_data = read.get("data", {})
        self.assertIsInstance(read_data, dict)
        self.assertIn("excerpt", read_data)
        self.assertIn("lineCount", read_data)


if __name__ == "__main__":
    unittest.main()
