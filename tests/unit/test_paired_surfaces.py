from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

import backend.db as db
import backend.main as main


class PairedSurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._sessions_before = dict(main.SESSIONS)
        self._identity_before = main._genome_identity
        self._persist_before = main.persist_sessions_to_disk_safe
        self._broadcast_before = main.broadcast_session
        self._dispatch_before = main._push_dispatch.dispatch
        self._relay_connected_before = main._mesh._relay_connected
        self._relay_route_before = main._mesh.relay_route
        self._db_path_before = db.get_db_path()
        main.SESSIONS.clear()
        self._tmpdir = tempfile.TemporaryDirectory()
        self._db_path = Path(self._tmpdir.name) / "paired-surfaces.db"
        asyncio.run(db.init(self._db_path))

        class _Identity:
            did = "did:example:test"

            def sign(self, data: bytes) -> str:
                return f"sig-{len(data)}"

        main._genome_identity = _Identity()

        async def _persist(_reason: str = "") -> bool:
            return True

        async def _broadcast(_session, _payload) -> None:
            return None

        async def _dispatch(tokens=None, msg_id: str = "", from_did: str = "", data=None) -> None:
            return None

        main.persist_sessions_to_disk_safe = _persist
        main.broadcast_session = _broadcast
        main._push_dispatch.dispatch = _dispatch
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
        asyncio.run(db.close())
        db._DB_PATH = self._db_path_before
        self._tmpdir.cleanup()

    def test_register_and_list_paired_surfaces(self) -> None:
        async def scenario() -> None:
            out = await main.register_paired_surface(
                main.SurfaceRegisterBody(
                    surfaceId="nous-phone",
                    label="Steve iPhone",
                    platform="ios",
                    role="mobile",
                    did="did:key:zstevephone",
                    relayUrl="wss://relay.genome.network:9090",
                    apns="apns-token",
                    sessionId="alpha",
                    capabilities=["takeover", "relay"],
                )
            )
            self.assertTrue(bool(out.get("ok", False)))
            listed = await main.list_paired_surfaces(limit=10)
            self.assertEqual(int(listed.get("count", 0) or 0), 1)
            item = listed["items"][0]
            self.assertEqual(item["surfaceId"], "nous-phone")
            self.assertEqual(item["role"], "mobile")
            self.assertEqual(item["did"], "did:key:zstevephone")

        asyncio.run(scenario())

    def test_handoff_prefers_paired_mobile_surface_when_session_presence_lacks_target(self) -> None:
        routed: list[dict[str, str]] = []

        async def _relay_route(to_did: str, envelope) -> None:
            payload = (envelope or {}).get("payload", {}) if isinstance((envelope or {}).get("payload", {}), dict) else {}
            routed.append(
                {
                    "to": str(to_did),
                    "type": str(payload.get("type", "")),
                    "sessionId": str(payload.get("sessionId", "")),
                }
            )

        main._mesh._relay_connected = True
        main._mesh.relay_route = _relay_route

        async def scenario() -> None:
            await main.register_paired_surface(
                main.SurfaceRegisterBody(
                    surfaceId="nous-phone",
                    label="Steve iPhone",
                    platform="ios",
                    role="mobile",
                    did="did:key:zstevephone",
                    relayUrl="wss://relay.genome.network:9090",
                    apns="apns-token",
                    sessionId="alpha",
                    capabilities=["takeover", "relay", "push"],
                )
            )
            session = main.ensure_session("alpha")
            session.presence = main.ensure_presence_state(
                {
                    "devices": {
                        "desktop-main": {
                            "deviceId": "desktop-main",
                            "label": "Genome Desktop",
                            "platform": "desktop",
                            "userAgent": "unit",
                            "lastSeenAt": 1,
                        }
                    }
                }
            )
            out = await main._handoff_start_impl("alpha", main.HandoffStartBody(deviceId="desktop-main"))
            await asyncio.sleep(0.01)
            self.assertFalse(bool(out.get("relayRouted", False)))
            paired = out.get("pairedSurface", {})
            self.assertTrue(bool(paired.get("routed", False)))
            self.assertEqual(str(paired.get("targetSurfaceId", "")), "nous-phone")
            self.assertEqual(len(routed), 1)
            self.assertEqual(routed[0]["to"], "did:key:zstevephone")
            self.assertEqual(routed[0]["type"], "genome_handoff")

        asyncio.run(scenario())

    def test_preferred_surface_and_explicit_target_override_handoff_selection(self) -> None:
        routed: list[str] = []

        async def _relay_route(to_did: str, envelope) -> None:
            routed.append(str(to_did))

        main._mesh._relay_connected = True
        main._mesh.relay_route = _relay_route

        async def scenario() -> None:
            await main.register_paired_surface(
                main.SurfaceRegisterBody(
                    surfaceId="phone-a",
                    label="Phone A",
                    platform="ios",
                    role="mobile",
                    did="did:key:za",
                    relayUrl="wss://relay.genome.network:9090",
                    preferred=True,
                )
            )
            await main.register_paired_surface(
                main.SurfaceRegisterBody(
                    surfaceId="phone-b",
                    label="Phone B",
                    platform="android",
                    role="mobile",
                    did="did:key:zb",
                    relayUrl="wss://relay.genome.network:9090",
                )
            )
            session = main.ensure_session("beta")
            session.presence = main.ensure_presence_state(
                {"devices": {"desktop-main": {"deviceId": "desktop-main", "label": "Desktop", "platform": "desktop", "lastSeenAt": 1}}}
            )

            preferred_out = await main._handoff_start_impl("beta", main.HandoffStartBody(deviceId="desktop-main"))
            self.assertEqual(str((preferred_out.get("pairedSurface") or {}).get("targetSurfaceId", "")), "phone-a")

            explicit_out = await main._handoff_start_impl(
                "beta",
                main.HandoffStartBody(deviceId="desktop-main", targetSurfaceId="phone-b"),
            )
            self.assertEqual(str((explicit_out.get("pairedSurface") or {}).get("targetSurfaceId", "")), "phone-b")
            self.assertIn("did:key:za", routed)
            self.assertIn("did:key:zb", routed)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
