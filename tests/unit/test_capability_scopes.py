from __future__ import annotations

import copy
import unittest

import backend.main as main


class CapabilityScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._vault_before = copy.deepcopy(main.CONNECTOR_VAULT)
        self._gcal_mode_before = main.GCAL_PROVIDER_MODE
        self._spotify_mode_before = main.SPOTIFY_PROVIDER_MODE

    def tearDown(self) -> None:
        main.CONNECTOR_VAULT = self._vault_before
        main.GCAL_PROVIDER_MODE = self._gcal_mode_before
        main.SPOTIFY_PROVIDER_MODE = self._spotify_mode_before

    def test_registry_exposes_connector_scope_metadata(self) -> None:
        spec = main.CAPABILITY_REGISTRY.get("web_search", {})
        scopes = spec.get("connector_scopes", []) if isinstance(spec.get("connector_scopes"), list) else []
        self.assertIn("web.page.read", scopes)

        capability = main.resolve_capability({"type": "web_search"})
        self.assertIn("web.page.read", capability.get("connector_scopes", []))

    def test_calendar_live_ops_require_granted_scope(self) -> None:
        main.CONNECTOR_VAULT = main.default_connector_vault_state()
        main.GCAL_PROVIDER_MODE = "live"

        policy = main.evaluate_policy(
            {"type": "calendar_create", "payload": {"title": "Standup"}},
            main.resolve_capability({"type": "calendar_create"}),
        )

        self.assertFalse(bool(policy.get("allowed", False)))
        self.assertEqual(str(policy.get("code", "")), "connector_scope_required")
        self.assertIn("https://www.googleapis.com/auth/calendar.events", policy.get("missingConnectorScopes", []))

    def test_connector_grants_report_lists_capabilities_for_scope(self) -> None:
        report = main.connector_grants_report()
        items = report.get("items", []) if isinstance(report.get("items"), list) else []
        web_item = next((item for item in items if item.get("scope") == "web.page.read"), None)
        self.assertIsNotNone(web_item)
        assert web_item is not None
        capabilities = web_item.get("capabilities", []) if isinstance(web_item.get("capabilities"), list) else []
        self.assertIn("web_search", capabilities)
        self.assertIn("fetch_url", capabilities)


if __name__ == "__main__":
    unittest.main()
