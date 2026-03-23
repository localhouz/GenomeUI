from __future__ import annotations

import asyncio
import copy
import unittest

import backend.main as main


class HandoffPushTests(unittest.TestCase):
    def setUp(self) -> None:
        self._sessions_before = dict(main.SESSIONS)
        self._identity_before = main._genome_identity
        self._persist_before = main.persist_sessions_to_disk_safe
        self._broadcast_before = main.broadcast_session
        self._dispatch_before = main._push_dispatch.dispatch
        self._relay_connected_before = main._mesh._relay_connected
        self._relay_route_before = main._mesh.relay_route
        main.SESSIONS.clear()

        class _Identity:
            did = "did:example:test"

            def sign(self, data: bytes) -> str:
                return f"sig-{len(data)}"

        main._genome_identity = _Identity()

        async def _persist(_reason: str = "") -> bool:
            return True

        async def _broadcast(_session, _payload) -> None:
            return None

        main.persist_sessions_to_disk_safe = _persist
        main.broadcast_session = _broadcast
        main._mesh._relay_connected = False

    def tearDown(self) -> None:
        main.SESSIONS.clear()
        main.SESSIONS.update(self._sessions_before)
        main._genome_identity = self._identity_before
        main.persist_sessions_to_disk_safe = self._persist_before
        main.broadcast_session = self._broadcast_before
        main._push_dispatch.dispatch = self._dispatch_before
        main._mesh._relay_connected = self._relay_connected_before
        main._mesh.relay_route = self._relay_route_before

    def test_register_push_tokens_stores_device_tokens(self) -> None:
        async def scenario() -> None:
            session = main.ensure_session("handoff-push-session")
            session.presence = main.ensure_presence_state({
                "devices": {
                    "dev-mobile": {
                        "deviceId": "dev-mobile",
                        "label": "Phone",
                        "platform": "ios",
                        "userAgent": "unit",
                        "lastSeenAt": 1,
                    }
                }
            })
            out = await main.register_session_push_tokens(
                "handoff-push-session",
                main.PushTokenBody(deviceId="dev-mobile", apns="apns-token", fcm="fcm-token"),
            )
            self.assertTrue(bool(out.get("ok", False)))
            stored = session.presence["devices"]["dev-mobile"]["pushTokens"]
            self.assertEqual(stored["apns"], "apns-token")
            self.assertEqual(stored["fcm"], "fcm-token")

        asyncio.run(scenario())

    def test_handoff_start_dispatches_wakeup_to_other_devices(self) -> None:
        dispatched: list[dict[str, str]] = []

        async def _dispatch(tokens, msg_id: str, from_did: str, data=None) -> None:
            dispatched.append({
                "apns": str(tokens.get("apns", "")),
                "fcm": str(tokens.get("fcm", "")),
                "msg_id": msg_id,
                "from_did": from_did,
                "type": str((data or {}).get("type", "")),
                "session_id": str((data or {}).get("sessionId", "")),
                "token": str((data or {}).get("token", "")),
                "backend_url": str((data or {}).get("backendUrl", "")),
            })

        main._push_dispatch.dispatch = _dispatch

        async def scenario() -> None:
            session = main.ensure_session("handoff-start-session")
            session.presence = main.ensure_presence_state({
                "devices": {
                    "dev-desktop": {
                        "deviceId": "dev-desktop",
                        "label": "Desktop",
                        "platform": "desktop",
                        "userAgent": "unit",
                        "lastSeenAt": 1,
                    },
                    "dev-mobile": {
                        "deviceId": "dev-mobile",
                        "label": "Phone",
                        "platform": "ios",
                        "userAgent": "unit",
                        "lastSeenAt": 1,
                        "pushTokens": {"apns": "ios-token", "fcm": ""},
                    },
                }
            })
            out = await main._handoff_start_impl("handoff-start-session", main.HandoffStartBody(deviceId="dev-desktop"))
            await asyncio.sleep(0.01)
            self.assertTrue(bool(out.get("ok", False)))
            self.assertTrue(bool(str(out.get("backendUrl", "")).strip()))
            self.assertEqual(len(dispatched), 1)
            self.assertEqual(dispatched[0]["apns"], "ios-token")
            self.assertEqual(dispatched[0]["from_did"], "did:example:test")
            self.assertIn("handoff:", dispatched[0]["msg_id"])
            self.assertEqual(dispatched[0]["type"], "genome_handoff")
            self.assertEqual(dispatched[0]["session_id"], "handoff-start-session")
            self.assertEqual(dispatched[0]["token"], str(out.get("token", "")))
            self.assertTrue(bool(dispatched[0]["backend_url"]))

        asyncio.run(scenario())

    def test_handoff_start_routes_takeover_to_mobile_did_when_relay_connected(self) -> None:
        routed: list[dict[str, str]] = []

        async def _relay_route(to_did: str, envelope) -> None:
            routed.append({
                "to": str(to_did),
                "type": str(((envelope or {}).get("payload") or {}).get("type", "")),
                "session_id": str(((envelope or {}).get("payload") or {}).get("sessionId", "")),
                "token": str(((envelope or {}).get("payload") or {}).get("token", "")),
            })

        async def _dispatch(_tokens, _msg_id: str, _from_did: str, data=None) -> None:
            return None

        main._mesh._relay_connected = True
        main._mesh.relay_route = _relay_route
        main._push_dispatch.dispatch = _dispatch

        async def scenario() -> None:
            session = main.ensure_session("handoff-relay-session")
            session.presence = main.ensure_presence_state({
                "devices": {
                    "dev-desktop": {
                        "deviceId": "dev-desktop",
                        "label": "Desktop",
                        "platform": "desktop",
                        "userAgent": "unit",
                        "lastSeenAt": 1,
                    },
                    "dev-mobile": {
                        "deviceId": "dev-mobile",
                        "label": "Nous iPhone",
                        "platform": "ios",
                        "userAgent": "unit",
                        "lastSeenAt": 1,
                        "did": "did:key:zmobile",
                    },
                }
            })
            out = await main._handoff_start_impl("handoff-relay-session", main.HandoffStartBody(deviceId="dev-desktop"))
            await asyncio.sleep(0.01)
            self.assertTrue(bool(out.get("relayRouted", False)))
            self.assertEqual(len(routed), 1)
            self.assertEqual(routed[0]["to"], "did:key:zmobile")
            self.assertEqual(routed[0]["type"], "genome_handoff")
            self.assertEqual(routed[0]["session_id"], "handoff-relay-session")
            self.assertEqual(routed[0]["token"], str(out.get("token", "")))

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
