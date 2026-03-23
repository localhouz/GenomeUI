from __future__ import annotations

import copy
import unittest
from unittest import mock

from fastapi.testclient import TestClient

import backend.main as main


class TurnPermissionContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._vault_before = copy.deepcopy(main.CONNECTOR_VAULT)
        self._sessions_before = copy.deepcopy(main.SESSIONS)
        main.CONNECTOR_VAULT = main.default_connector_vault_state()
        self.client = TestClient(main.app)

    def tearDown(self) -> None:
        main.CONNECTOR_VAULT = self._vault_before
        main.SESSIONS.clear()
        main.SESSIONS.update(self._sessions_before)
        self.client.close()

    def test_turn_returns_http_403_for_missing_connector_scope(self) -> None:
        with mock.patch.object(main._auth, "session_valid", return_value=True):
            response = self.client.post(
                "/api/turn",
                json={"sessionId": "ut-turn-scope-403", "intent": "search web local-first operating system"},
            )

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(str(payload.get("code", "")), "connector_scope_required")
        self.assertEqual(str(payload.get("blockedOp", "")), "web_search")
        self.assertIn("web.page.read", payload.get("missingConnectorScopes", []))
        execution = payload.get("execution", {}) if isinstance(payload.get("execution"), dict) else {}
        self.assertFalse(bool(execution.get("ok", True)))
        self.assertIn("plan", payload)
        self.assertIn("envelope", payload)

    def test_idempotent_replay_preserves_http_403_for_scope_denial(self) -> None:
        body = {
            "sessionId": "ut-turn-scope-403-replay",
            "intent": "search web local-first operating system",
            "idempotencyKey": "ut:blocked-turn",
        }
        with mock.patch.object(main._auth, "session_valid", return_value=True):
            first = self.client.post("/api/turn", json=body)
            replay = self.client.post("/api/turn", json=body)

        self.assertEqual(first.status_code, 403)
        self.assertEqual(replay.status_code, 403)
        payload = replay.json()
        self.assertEqual(str(payload.get("code", "")), "connector_scope_required")
        idem = payload.get("idempotency", {}) if isinstance(payload.get("idempotency"), dict) else {}
        self.assertTrue(bool(idem.get("reused", False)))


if __name__ == "__main__":
    unittest.main()
