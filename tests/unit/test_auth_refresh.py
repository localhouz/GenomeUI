from __future__ import annotations

import asyncio
import os
import time
import unittest
from unittest.mock import patch

from backend import auth


class AuthRefreshUnitTests(unittest.TestCase):
    def test_refresh_token_if_needed_sync_returns_fresh_token_without_http(self) -> None:
        fresh = {
            "access_token": "fresh-token",
            "refresh_token": "refresh-token",
            "expires_at": time.time() + 600,
        }

        with patch.object(auth, "vault_retrieve", return_value=fresh), \
             patch("backend.auth.httpx.post", side_effect=AssertionError("refresh should not run")):
            result = auth.refresh_token_if_needed_sync("gmail")

        self.assertEqual(result["access_token"], "fresh-token")

    def test_refresh_token_if_needed_sync_refreshes_google_token(self) -> None:
        stale = {
            "access_token": "old-token",
            "refresh_token": "refresh-token",
            "expires_at": time.time() - 30,
        }
        stored: dict[str, object] = {}

        class FakeResponse:
            status_code = 200

            @staticmethod
            def json() -> dict[str, object]:
                return {
                    "access_token": "new-token",
                    "refresh_token": "new-refresh",
                    "expires_in": 1800,
                }

        with patch.dict(os.environ, {"GMAIL_CLIENT_ID": "gmail-client", "GMAIL_CLIENT_SECRET": "gmail-secret"}, clear=False), \
             patch.object(auth, "vault_retrieve", return_value=dict(stale)), \
             patch.object(auth, "vault_store", side_effect=lambda service, token: stored.update({"service": service, "token": token})), \
             patch("backend.auth.httpx.post", return_value=FakeResponse()) as post_mock:
            result = auth.refresh_token_if_needed_sync("gmail")

        self.assertEqual(result["access_token"], "new-token")
        self.assertEqual(result["refresh_token"], "new-refresh")
        self.assertEqual(stored["service"], "gmail")
        self.assertEqual(stored["token"]["access_token"], "new-token")
        self.assertEqual(post_mock.call_args.kwargs["data"]["client_secret"], "gmail-secret")

    def test_refresh_token_if_needed_sync_uses_pkce_style_spotify_refresh(self) -> None:
        stale = {
            "access_token": "old-token",
            "refresh_token": "refresh-token",
            "expires_at": time.time() - 30,
        }

        class FakeResponse:
            status_code = 200

            @staticmethod
            def json() -> dict[str, object]:
                return {
                    "access_token": "spotify-new-token",
                    "expires_in": 3600,
                }

        with patch.dict(os.environ, {"SPOTIFY_CLIENT_ID": "spotify-client", "SPOTIFY_CLIENT_SECRET": "spotify-secret"}, clear=False), \
             patch.object(auth, "vault_retrieve", return_value=dict(stale)), \
             patch.object(auth, "vault_store"), \
             patch("backend.auth.httpx.post", return_value=FakeResponse()) as post_mock:
            result = auth.refresh_token_if_needed_sync("spotify")

        payload = post_mock.call_args.kwargs["data"]
        self.assertEqual(result["access_token"], "spotify-new-token")
        self.assertEqual(payload["client_id"], "spotify-client")
        self.assertNotIn("client_secret", payload)

    def test_refresh_token_if_needed_async_wraps_sync_helper(self) -> None:
        with patch.object(auth, "refresh_token_if_needed_sync", return_value={"access_token": "wrapped-token"}):
            result = asyncio.run(auth.refresh_token_if_needed("gmail"))

        self.assertEqual(result["access_token"], "wrapped-token")


if __name__ == "__main__":
    unittest.main()
