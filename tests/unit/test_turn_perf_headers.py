from __future__ import annotations

import asyncio
import types
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import Response

import backend.main as main


class TurnPerfHeaderUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self._sessions_before = dict(main.SESSIONS)

    def tearDown(self) -> None:
        main.SESSIONS.clear()
        main.SESSIONS.update(self._sessions_before)

    def test_turn_sets_perf_headers(self) -> None:
        async def run_case() -> None:
            req = types.SimpleNamespace(client=types.SimpleNamespace(host="127.0.0.1"))
            resp = Response()
            body = main.TurnBody(
                intent="show weather in chicago",
                sessionId="perfheaders",
                nousIntent={"op": "weather_forecast", "slots": {"location": "Chicago"}, "_nousMs": 12},
            )

            with patch.object(main._auth, "session_valid", return_value=True):
                payload = await main.turn(req, body, resp, x_genome_auth=None)

            self.assertTrue(bool(payload.get("sessionId")))
            self.assertEqual(resp.headers.get("X-Genome-Classify-Ms"), "12")
            self.assertEqual(resp.headers.get("X-Genome-Nous-Ms"), "12")
            self.assertEqual(resp.headers.get("X-Genome-Classify-Source"), "nous_gateway")
            self.assertIsNotNone(resp.headers.get("X-Genome-Parse-Ms"))
            self.assertIsNotNone(resp.headers.get("X-Genome-Total-Ms"))
            self.assertEqual(resp.headers.get("X-Nous-Parse-Ms"), "12")
            self.assertEqual(resp.headers.get("X-Nous-Classify-Source"), "nous_gateway")

        asyncio.run(run_case())

    def test_turn_fast_path_skips_embedded_nous_for_weather(self) -> None:
        async def run_case() -> None:
            req = types.SimpleNamespace(client=types.SimpleNamespace(host="127.0.0.1"))
            resp = Response()
            body = main.TurnBody(
                intent="show weather in chicago",
                sessionId="fastpathheaders",
            )

            with patch.object(main._auth, "session_valid", return_value=True), \
                 patch.object(main._nous, "is_loaded", return_value=True), \
                 patch.object(main._nous, "classify", AsyncMock(side_effect=AssertionError("embedded Nous should not run"))):
                payload = await main.turn(req, body, resp, x_genome_auth=None)

            self.assertTrue(bool(payload.get("sessionId")))
            self.assertEqual(resp.headers.get("X-Genome-Classify-Source"), "rule_fast_path")
            self.assertEqual(resp.headers.get("X-Genome-Nous-Ms"), "0")
            self.assertEqual(resp.headers.get("X-Nous-Parse-Ms"), "0")

        asyncio.run(run_case())


if __name__ == "__main__":
    unittest.main()
