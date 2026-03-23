from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from backend import semantics


class SemanticsTimeoutUnitTests(unittest.TestCase):
    def test_classify_async_uses_fast_path_before_nous_http(self) -> None:
        async def run_case() -> None:
            class ShouldNotCallClient:
                def __init__(self, *args, **kwargs):
                    raise AssertionError("Nous HTTP client should not be used for fast-path intents")

            with patch.object(semantics, "NOUS_URL", "http://127.0.0.1:7700"), \
                 patch("httpx.AsyncClient", ShouldNotCallClient):
                match = await semantics.classify_async("show weather today", timeout_s=0.01)

            self.assertIsNotNone(match)
            assert match is not None
            self.assertEqual(match.op, "weather_forecast")

        asyncio.run(run_case())

    def test_classify_async_falls_back_to_rules_when_nous_times_out(self) -> None:
        async def run_case() -> None:
            class FakeClient:
                def __init__(self, *args, **kwargs):
                    pass

                async def __aenter__(self):
                    return self

                async def __aexit__(self, exc_type, exc, tb):
                    return False

                async def post(self, *args, **kwargs):
                    raise TimeoutError("simulated timeout")

            with patch.object(semantics, "NOUS_URL", "http://127.0.0.1:7700"), \
                 patch("httpx.AsyncClient", FakeClient):
                match = await semantics.classify_async("show weather today", timeout_s=0.01)

            self.assertIsNotNone(match)
            assert match is not None
            self.assertEqual(match.op, "weather_forecast")

        asyncio.run(run_case())


if __name__ == "__main__":
    unittest.main()
