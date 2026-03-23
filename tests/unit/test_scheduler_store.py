import asyncio
import pathlib
import shutil
import unittest
import uuid

from backend import db
from backend import scheduler as sched


class SchedulerStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._db_path_before = db.get_db_path()
        self._tmp_root = pathlib.Path(".test-temp") / f"scheduler-db-{uuid.uuid4().hex}"
        self._tmp_root.mkdir(parents=True, exist_ok=True)
        self.db_path = self._tmp_root / "scheduler.db"
        asyncio.run(db.init(self.db_path))
        sched._history.clear()
        sched._paused.clear()

    def tearDown(self) -> None:
        asyncio.run(sched.stop())
        asyncio.run(db.close())
        shutil.rmtree(self._tmp_root, ignore_errors=True)
        db._DB_PATH = self._db_path_before

    def test_scheduler_runs_persist_in_sqlite(self) -> None:
        async def scenario() -> None:
            await db.save_scheduler_run("gmail_sync", True, "gmail ok", {"count": 2}, created_at=100)
            await db.save_scheduler_run("gmail_sync", False, "gmail failed", {"count": 0}, created_at=200)
            rows = await db.list_scheduler_runs("gmail_sync", limit=5)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["detail"], "gmail failed")
            self.assertFalse(rows[0]["ok"])
            self.assertEqual(rows[1]["data"]["count"], 2)

        asyncio.run(scenario())

    def test_scheduler_status_reads_persisted_history(self) -> None:
        class _FakeJob:
            id = "gmail_sync"
            name = "Gmail sync"
            next_run_time = None

        class _FakeScheduler:
            running = True

            def get_jobs(self):
                return [_FakeJob()]

        async def scenario() -> None:
            await db.save_scheduler_run("gmail_sync", True, "persisted snapshot", {"count": 1}, created_at=500)
            prior = sched._scheduler
            sched._scheduler = _FakeScheduler()
            try:
                status = await sched.get_status_async()
            finally:
                sched._scheduler = prior
            jobs = {str(job.get("id")): job for job in status.get("jobs", [])}
            self.assertIn("gmail_sync", jobs)
            history = jobs["gmail_sync"].get("history", [])
            self.assertGreaterEqual(len(history), 1)
            self.assertEqual(history[0]["detail"], "persisted snapshot")
            self.assertEqual(history[0]["data"]["count"], 1)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
