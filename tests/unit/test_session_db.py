from __future__ import annotations

import asyncio
import json
import pathlib
import shutil
import unittest
import uuid

from backend import db
import backend.main as main


class SessionDbUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self._db_path_before = db.get_db_path()
        self._sessions_before = dict(main.SESSIONS)
        self._legacy_path_before = main.LEGACY_SESSION_JSON_PATH
        self._tmp_root = pathlib.Path(".test-temp") / f"session-db-{uuid.uuid4().hex}"
        self._tmp_root.mkdir(parents=True, exist_ok=True)
        self._db_path = self._tmp_root / "sessions.db"
        asyncio.run(db.init(self._db_path))

    def tearDown(self) -> None:
        main.SESSIONS.clear()
        main.SESSIONS.update(self._sessions_before)
        main.LEGACY_SESSION_JSON_PATH = self._legacy_path_before
        asyncio.run(db.close())
        shutil.rmtree(self._tmp_root, ignore_errors=True)
        asyncio.run(db.init(self._db_path_before))
        asyncio.run(db.close())

    def test_save_and_load_single_session(self) -> None:
        asyncio.run(db.save_session("alpha", {"revision": 2, "memory": {"foo": "bar"}}))

        loaded = asyncio.run(db.load_session("alpha"))

        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(int(loaded.get("revision", 0)), 2)
        self.assertEqual((loaded.get("memory") or {}).get("foo"), "bar")

    def test_list_sessions_orders_recent_first(self) -> None:
        asyncio.run(db.save_session("older", {"revision": 1}))
        asyncio.run(db.save_session("newer", {"revision": 2}))

        ids = asyncio.run(db.list_sessions())

        self.assertEqual(ids[:2], ["newer", "older"])

    def test_save_all_replaces_removed_sessions(self) -> None:
        asyncio.run(db.save_all({"keep": {"revision": 1}, "drop": {"revision": 1}}))
        asyncio.run(db.save_all({"keep": {"revision": 2}}))

        ids = asyncio.run(db.list_sessions())
        keep = asyncio.run(db.load_session("keep"))
        drop = asyncio.run(db.load_session("drop"))

        self.assertEqual(ids, ["keep"])
        self.assertEqual(int((keep or {}).get("revision", 0)), 2)
        self.assertIsNone(drop)

    def test_backend_restart_round_trip_restores_session_state(self) -> None:
        async def run_case() -> None:
            session = main.ensure_session("restart-me")
            session.revision = 7
            session.memory["notes"] = [{"title": "Persisted"}]
            session.notifications = [{"id": "notif-1", "type": "info", "message": "hello"}]

            await main.persist_sessions_to_disk()

            main.SESSIONS.clear()
            await db.close()
            await db.init(self._db_path)
            await main.load_sessions_from_disk()

            restored = main.SESSIONS.get("restart-me")
            self.assertIsNotNone(restored)
            assert restored is not None
            self.assertEqual(restored.revision, 7)
            self.assertEqual(restored.memory["notes"][0]["title"], "Persisted")
            self.assertEqual(restored.notifications[0]["message"], "hello")
            self.assertIsNone(restored.last_turn)

        asyncio.run(run_case())

    def test_legacy_json_migrates_into_sqlite_and_renames_file(self) -> None:
        async def run_case() -> None:
            main.SESSIONS.clear()
            legacy_session = main.ensure_session("migrate-me")
            legacy_session.revision = 3
            legacy_session.memory["notes"] = [{"title": "Imported"}]
            payload = main.serialize_session_state(legacy_session)
            legacy_path = self._tmp_root / "sessions.json"
            legacy_path.write_text(
                json.dumps({"sessions": {"migrate-me": payload}}),
                encoding="utf-8",
            )
            migrated_path = legacy_path.with_suffix(".json.migrated")

            main.SESSIONS.clear()
            main.LEGACY_SESSION_JSON_PATH = legacy_path

            await main.load_sessions_from_disk()

            restored = main.SESSIONS.get("migrate-me")
            self.assertFalse(legacy_path.exists())
            self.assertTrue(migrated_path.exists())
            self.assertIsNotNone(restored)
            assert restored is not None
            self.assertEqual(restored.revision, 3)
            self.assertEqual(restored.memory["notes"][0]["title"], "Imported")
            self.assertEqual(await db.list_sessions(), ["migrate-me"])

        asyncio.run(run_case())

    def test_write_soak_handles_one_thousand_session_saves(self) -> None:
        async def run_case() -> None:
            for i in range(1000):
                await db.save_session(
                    f"session-{i:04d}",
                    {"revision": i, "memory": {"index": i}},
                )

            ids = await db.list_sessions()
            first = await db.load_session("session-0000")
            last = await db.load_session("session-0999")

            self.assertEqual(len(ids), 1000)
            self.assertEqual(int((first or {}).get("memory", {}).get("index", -1)), 0)
            self.assertEqual(int((last or {}).get("memory", {}).get("index", -1)), 999)

        asyncio.run(run_case())


if __name__ == "__main__":
    unittest.main()
