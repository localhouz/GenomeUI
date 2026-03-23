import time
import unittest
from unittest import mock

from backend import main


class _FakeSportsResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self):
        return {"source": "fresh"}


class _FakeSportsClient:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, *args, **kwargs):
        return _FakeSportsResponse()


class SportsCacheTtlTests(unittest.TestCase):
    def test_endpoint_specific_ttls_are_exposed(self) -> None:
        self.assertEqual(main._espn_cache_ttl_for("scoreboard"), main._ESPN_CACHE_TTL_SCOREBOARD_S)
        self.assertEqual(main._espn_cache_ttl_for("standings"), main._ESPN_CACHE_TTL_STANDINGS_S)
        self.assertEqual(main._espn_cache_ttl_for("teams"), main._ESPN_CACHE_TTL_DEFAULT_S)

    def test_scoreboard_cache_expires_on_scoreboard_ttl(self) -> None:
        cache_key = "nba:scoreboard"
        old_cache = dict(main._ESPN_CACHE)
        try:
            main._ESPN_CACHE.clear()
            main._ESPN_CACHE[cache_key] = (
                time.monotonic() - main._ESPN_CACHE_TTL_SCOREBOARD_S - 1.0,
                {"ok": True, "source": "cached"},
            )
            with mock.patch.object(main.httpx, "Client", _FakeSportsClient):
                result = main.espn_fetch("nba", "scoreboard")
            self.assertEqual(result.get("source"), "fresh")
            self.assertTrue(bool(result.get("ok", False)))
        finally:
            main._ESPN_CACHE.clear()
            main._ESPN_CACHE.update(old_cache)


if __name__ == "__main__":
    unittest.main()
