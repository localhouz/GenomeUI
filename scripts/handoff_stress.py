from __future__ import annotations

import pathlib
import sys
import time

from fastapi.testclient import TestClient

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
    sid = f"handoff-stress-{int(time.time())}"
    cycles = 100

    with TestClient(app) as client:
        post_json(client, "/api/session/init", {"sessionId": sid})

        last_claimed = ""
        for idx in range(cycles):
            src = f"desk-{idx % 2}"
            dst = f"phone-{idx % 2}"
            started = post_json(client, f"/api/session/{sid}/handoff/start", {"deviceId": src})
            token = str(started.get("token", "")).strip()
            assert_true(bool(token), f"cycle {idx}: missing handoff token")

            claimed = post_json(client, f"/api/session/{sid}/handoff/claim", {"deviceId": dst, "token": token})
            assert_true(claimed.get("activeDeviceId") == dst, f"cycle {idx}: active device should switch to claimant")
            assert_true((claimed.get("handoff") or {}).get("pending") is None, f"cycle {idx}: pending handoff should clear")
            last_claimed = dst

        final_state = get_json(client, f"/api/session/{sid}")
        handoff = final_state.get("handoff", {})
        assert_true(handoff.get("activeDeviceId") == last_claimed, "final active device should match last claim")
        assert_true(handoff.get("pending") is None, "final pending handoff should be empty")

    print("handoff stress passed")


if __name__ == "__main__":
    main()
