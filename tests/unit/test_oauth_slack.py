import asyncio
import json
import unittest
from unittest import mock

from backend import main


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class SlackOAuthTests(unittest.TestCase):
    def test_oauth_begin_returns_slack_authorize_url(self) -> None:
        with mock.patch.object(main._auth, "session_valid", return_value=True):
            with mock.patch.object(main, "_get_oauth_client_pair", return_value=("slack-client", "slack-secret")):
                response = asyncio.run(main.oauth_begin("slack", x_genome_auth="token"))
        body = json.loads(response.body)
        self.assertTrue(bool(body.get("ok", False)))
        self.assertEqual(str(body.get("service", "")), "slack")
        self.assertIn("slack.com/oauth/v2/authorize", str(body.get("url", "")))
        self.assertIn("client_id=slack-client", str(body.get("url", "")))

    def test_oauth_callback_stores_bot_and_user_tokens_for_slack(self) -> None:
        state = "slack-state-test"
        main._PKCE_STATE[state] = {
            "verifier": "verifier-123",
            "service": "slack",
            "redirect_uri": "http://localhost:5173/api/connectors/oauth/slack/callback",
        }
        stored = {}

        payload = {
            "ok": True,
            "access_token": "xoxb-bot-token",
            "scope": "channels:read,chat:write",
            "bot_user_id": "B123",
            "team": {"id": "T123", "name": "Genome"},
            "authed_user": {
                "id": "U123",
                "scope": "search:read,users.profile:write",
                "access_token": "xoxp-user-token",
            },
        }

        with mock.patch.object(main.httpx, "post", return_value=_FakeResponse(payload)):
            with mock.patch.object(main._auth, "vault_store", side_effect=lambda service, data: stored.update({"service": service, "data": data})):
                response = asyncio.run(main.oauth_callback("slack", code="abc", state=state, error=""))

        self.assertEqual(response.headers.get("location"), "/?oauth_success=slack")
        self.assertEqual(stored.get("service"), "slack")
        data = stored.get("data", {})
        self.assertEqual(data.get("access_token"), "xoxb-bot-token")
        self.assertEqual(data.get("user_access_token"), "xoxp-user-token")
        self.assertEqual(data.get("team_name"), "Genome")
        self.assertEqual(data.get("authed_user_id"), "U123")


if __name__ == "__main__":
    unittest.main()
