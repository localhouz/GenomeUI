from __future__ import annotations

import os
import pathlib
import sys
import time

from fastapi.testclient import TestClient

os.environ["GENOMEUI_CONNECTOR_PROVIDER_MODE"] = "mock"

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.main import app


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def post_json(client: TestClient, url: str, payload: dict) -> dict:
    resp = client.post(url, json=payload)
    assert_true(resp.status_code == 200, f"{url} failed: {resp.status_code} {resp.text}")
    return resp.json()


def get_json(client: TestClient, url: str) -> dict:
    resp = client.get(url)
    assert_true(resp.status_code == 200, f"{url} failed: {resp.status_code} {resp.text}")
    return resp.json()


def main() -> None:
    nonce = str(int(time.time() * 1000))[-8:]
    sid = f"connector-replay-{nonce}"
    with TestClient(app) as client:
        post_json(client, "/api/session/init", {"sessionId": sid})

        providers = get_json(client, "/api/connectors/providers")
        assert_true(providers.get("ok") is True, "providers endpoint should succeed")
        contracts = get_json(client, "/api/connectors/contracts")
        assert_true(contracts.get("ok") is True, "contracts endpoint should succeed")
        provider_contracts = (contracts.get("contracts") or {}).get("providers", {})
        assert_true("weather" in provider_contracts and "banking" in provider_contracts and "social" in provider_contracts and "telephony" in provider_contracts, "contracts should include core providers")
        weather_provider = (providers.get("providers") or {}).get("weather", {})
        assert_true(str(weather_provider.get("mode", "")) == "mock", "weather provider should be in mock mode")
        banking_provider = (providers.get("providers") or {}).get("banking", {})
        social_provider = (providers.get("providers") or {}).get("social", {})
        telephony_provider = (providers.get("providers") or {}).get("telephony", {})
        assert_true(str(banking_provider.get("mode", "")) == "scaffold", "banking provider should be scaffold mode")
        assert_true(str(social_provider.get("mode", "")) == "scaffold", "social provider should be scaffold mode")
        assert_true(str(telephony_provider.get("mode", "")) == "scaffold", "telephony provider should be scaffold mode")
        assert_true(isinstance(banking_provider.get("configured"), bool), "banking provider should expose configured flag")
        assert_true(isinstance(social_provider.get("configured"), bool), "social provider should expose configured flag")
        secrets_before = get_json(client, "/api/connectors/secrets")
        assert_true(secrets_before.get("ok") is True, "connector secrets endpoint should succeed")
        assert_true(isinstance(secrets_before.get("items", []), list), "connector secrets endpoint should return items")

        mock_weather = get_json(client, "/api/connectors/mock/weather?location=Seattle")
        item = mock_weather.get("item", {})
        assert_true(str(item.get("source", "")) == "mock", "mock weather endpoint should return mock source")
        assert_true(float(item.get("temperatureF", 0.0) or 0.0) > 0.0, "mock weather should include temperature")
        mock_banking = get_json(client, "/api/connectors/mock/banking?limit=3")
        assert_true(mock_banking.get("ok") is True, "mock banking endpoint should succeed")
        assert_true(bool((mock_banking.get("transactions") or {}).get("items", [])), "mock banking should include transactions")
        mock_social = get_json(client, "/api/connectors/mock/social")
        assert_true(mock_social.get("ok") is True, "mock social endpoint should succeed")
        assert_true(bool((mock_social.get("feed") or {}).get("items", [])), "mock social should include feed items")

        # Ensure deterministic baseline even if local vault persisted prior grants.
        post_json(client, "/api/turn", {"sessionId": sid, "intent": "revoke weather forecast"})
        blocked = post_json(client, "/api/turn", {"sessionId": sid, "intent": "show weather in Seattle"})
        blocked_tool = blocked["execution"]["toolResults"][0]
        assert_true(not blocked["execution"]["ok"], "weather should be blocked without grant")
        assert_true(blocked_tool.get("policy", {}).get("code") == "connector_scope_required", "weather should require scope grant")

        granted = post_json(client, "/api/turn", {"sessionId": sid, "intent": "grant weather forecast"})
        assert_true(granted["execution"]["ok"], "grant weather forecast should succeed")

        weather_one = post_json(client, "/api/turn", {"sessionId": sid, "intent": "show weather in Seattle"})
        tool_one = weather_one["execution"]["toolResults"][0]
        assert_true(weather_one["execution"]["ok"], "weather call should succeed after grant")
        assert_true(any("source: mock" in str(line).lower() for line in tool_one.get("previewLines", [])), "weather preview should report mock source")

        weather_two = post_json(client, "/api/turn", {"sessionId": sid, "intent": "show weather in Seattle"})
        tool_two = weather_two["execution"]["toolResults"][0]
        t1 = [str(line) for line in tool_one.get("previewLines", [])]
        t2 = [str(line) for line in tool_two.get("previewLines", [])]
        assert_true(t1 == t2, "mock weather output should be deterministic between runs")

        revoke = post_json(client, "/api/turn", {"sessionId": sid, "intent": "revoke weather forecast"})
        assert_true(revoke["execution"]["ok"], "revoke weather forecast should succeed")
        blocked_again = post_json(client, "/api/turn", {"sessionId": sid, "intent": "show weather in Seattle"})
        assert_true(not blocked_again["execution"]["ok"], "weather should be blocked again after revoke")

    print("connector replay passed")


if __name__ == "__main__":
    main()
