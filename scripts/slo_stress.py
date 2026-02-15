from __future__ import annotations

import pathlib
import sys
import time

from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import backend.main as backend_main


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def post_json(client: TestClient, url: str, payload: dict) -> dict:
    resp = client.post(url, json=payload)
    assert_true(resp.status_code == 200, f"{url} failed: {resp.status_code} {resp.text}")
    return resp.json()


def main() -> None:
    sid = f"slo-stress-{int(time.time() * 1000)}"
    old_budget = backend_main.TURN_LATENCY_BUDGET_MS
    old_streak = backend_main.SLO_BREACH_STREAK_FOR_THROTTLE
    old_window = backend_main.SLO_THROTTLE_MS
    try:
        # Force deterministic breach behavior for test coverage.
        backend_main.TURN_LATENCY_BUDGET_MS = -1
        backend_main.SLO_BREACH_STREAK_FOR_THROTTLE = 2
        backend_main.SLO_THROTTLE_MS = 120_000

        with TestClient(backend_main.app) as client:
            post_json(client, "/api/session/init", {"sessionId": sid})

            turn1 = post_json(client, "/api/turn", {"sessionId": sid, "intent": "add note slo probe 1"})
            slo1 = ((turn1.get("kernelTrace", {}).get("runtime", {}) or {}).get("slo", {}) or {})
            assert_true(int(slo1.get("breachStreak", 0)) >= 1, "first turn should increment SLO breach streak")
            assert_true(not bool(slo1.get("throttled", False)), "first breach should not throttle yet")

            turn2 = post_json(client, "/api/turn", {"sessionId": sid, "intent": "add note slo probe 2"})
            slo2 = ((turn2.get("kernelTrace", {}).get("runtime", {}) or {}).get("slo", {}) or {})
            assert_true(int(slo2.get("breachStreak", 0)) >= 2, "second turn should hit breach streak threshold")
            assert_true(bool(slo2.get("throttled", False)), "second breach should enable throttling")

            turn3 = post_json(client, "/api/turn", {"sessionId": sid, "intent": "show tasks"})
            route3 = turn3.get("route", {}) or {}
            assert_true(str(route3.get("reason", "")) == "slo_throttle", "throttled session should force deterministic route")

            session = client.get(f"/api/session/{sid}").json()
            throttled = int((session.get("slo", {}) or {}).get("throttleUntil", 0) or 0)
            assert_true(throttled > backend_main.now_ms(), "session SLO state should persist active throttle window")
    finally:
        backend_main.TURN_LATENCY_BUDGET_MS = old_budget
        backend_main.SLO_BREACH_STREAK_FOR_THROTTLE = old_streak
        backend_main.SLO_THROTTLE_MS = old_window

    print("slo stress passed")


if __name__ == "__main__":
    main()
