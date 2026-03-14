"""
Genome Push Dispatch — APNs (iOS) + FCM (Android) push notifications.

Sends silent background push notifications to wake sleeping mobile devices
so they can connect to the relay and drain their message queue.

NO message content is included in the push payload — only a wakeup signal
and the relay URL. The device fetches the actual messages after connecting.

APNs setup:
  Set APNS_KEY_ID, APNS_TEAM_ID, APNS_KEY_P8_PATH, APNS_BUNDLE_ID
  in the environment (or .env). Requires an Apple Developer account.

FCM setup:
  Set FCM_SERVICE_ACCOUNT to the path of a Firebase service account JSON.
  Requires a Firebase project with Cloud Messaging enabled.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from typing import TypedDict

_log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

_APNS_KEY_ID    = os.environ.get("APNS_KEY_ID", "")
_APNS_TEAM_ID   = os.environ.get("APNS_TEAM_ID", "")
_APNS_KEY_P8    = os.environ.get("APNS_KEY_P8_PATH", "")
_APNS_BUNDLE_ID = os.environ.get("APNS_BUNDLE_ID", "")
_APNS_SANDBOX   = os.environ.get("APNS_SANDBOX", "false").lower() == "true"

_FCM_SERVICE_ACCOUNT = os.environ.get("FCM_SERVICE_ACCOUNT", "")

# Relay WebSocket URL advertised in the push payload so the device knows where to connect
_RELAY_PUBLIC_URL = os.environ.get("GENOME_RELAY_PUBLIC_URL", "")


class PushTokens(TypedDict, total=False):
    apns: str    # APNs device token (hex string)
    fcm: str     # FCM registration token


# ── APNs JWT cache ────────────────────────────────────────────────────────────

_apns_jwt: str = ""
_apns_jwt_issued_at: float = 0.0


def _apns_jwt_token() -> str:
    """Return a cached APNs JWT, refreshing if older than 50 minutes."""
    global _apns_jwt, _apns_jwt_issued_at

    now = time.time()
    if _apns_jwt and (now - _apns_jwt_issued_at) < 50 * 60:
        return _apns_jwt

    try:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import hashes

        with open(_APNS_KEY_P8, "rb") as f:
            private_key = load_pem_private_key(f.read(), password=None)

        iat = int(now)
        header  = base64.urlsafe_b64encode(json.dumps({"alg": "ES256", "kid": _APNS_KEY_ID}).encode()).rstrip(b"=").decode()
        payload = base64.urlsafe_b64encode(json.dumps({"iss": _APNS_TEAM_ID, "iat": iat}).encode()).rstrip(b"=").decode()
        to_sign = f"{header}.{payload}".encode()

        # DER signature → IEEE-P1363 (r || s, each 32 bytes)
        der_sig = private_key.sign(to_sign, ec.ECDSA(hashes.SHA256()))  # type: ignore[arg-type]
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
        r, s = decode_dss_signature(der_sig)
        sig_bytes = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        sig = base64.urlsafe_b64encode(sig_bytes).rstrip(b"=").decode()

        _apns_jwt = f"{header}.{payload}.{sig}"
        _apns_jwt_issued_at = now
        return _apns_jwt

    except Exception as exc:
        _log.error("APNs JWT generation failed: %s", exc)
        return ""


# ── APNs send ─────────────────────────────────────────────────────────────────

async def send_apns(device_token: str, msg_id: str, from_did: str) -> bool:
    """Send a silent APNs push. Returns True on success."""
    if not all([_APNS_KEY_ID, _APNS_TEAM_ID, _APNS_KEY_P8, _APNS_BUNDLE_ID, device_token]):
        return False

    jwt = _apns_jwt_token()
    if not jwt:
        return False

    host = "api.sandbox.push.apple.com" if _APNS_SANDBOX else "api.push.apple.com"
    path = f"/3/device/{device_token}"

    push_payload = json.dumps({
        "aps": {"content-available": 1},
        "msgId": msg_id,
        "from": from_did,
        "relayUrl": _RELAY_PUBLIC_URL,
    }).encode()

    headers = {
        "authorization": f"bearer {jwt}",
        "apns-push-type": "background",
        "apns-priority": "5",           # silent push must use priority 5
        "apns-topic": _APNS_BUNDLE_ID,
        "content-type": "application/json",
        "content-length": str(len(push_payload)),
    }

    try:
        import httpx
        async with httpx.AsyncClient(http2=True) as client:
            r = await client.post(
                f"https://{host}{path}",
                content=push_payload,
                headers=headers,
                timeout=10.0,
            )
            if r.status_code == 200:
                _log.debug("APNs push sent: %s → %s", msg_id, device_token[:8])
                return True
            _log.warning("APNs push failed: %s %s", r.status_code, r.text)
            return False
    except ImportError:
        _log.warning("httpx not installed — APNs push skipped. Install: pip install httpx[http2]")
        return False
    except Exception as exc:
        _log.error("APNs push error: %s", exc)
        return False


# ── FCM send ──────────────────────────────────────────────────────────────────

async def send_fcm(fcm_token: str, msg_id: str, from_did: str) -> bool:
    """Send a silent FCM data message. Returns True on success."""
    if not _FCM_SERVICE_ACCOUNT or not fcm_token:
        return False

    try:
        # firebase-admin is synchronous — run in thread pool
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _send_fcm_sync, fcm_token, msg_id, from_did)
    except Exception as exc:
        _log.error("FCM push error: %s", exc)
        return False


def _send_fcm_sync(fcm_token: str, msg_id: str, from_did: str) -> bool:
    try:
        import firebase_admin  # type: ignore[import]
        from firebase_admin import credentials, messaging

        if not firebase_admin._apps:  # type: ignore[attr-defined]
            cred = credentials.Certificate(_FCM_SERVICE_ACCOUNT)
            firebase_admin.initialize_app(cred)

        message = messaging.Message(
            token=fcm_token,
            android=messaging.AndroidConfig(priority="high"),
            data={
                "type": "genome_wakeup",
                "msgId": msg_id,
                "from": from_did,
                "relayUrl": _RELAY_PUBLIC_URL,
            },
        )
        messaging.send(message)
        _log.debug("FCM push sent: %s → %s", msg_id, fcm_token[:8])
        return True
    except ImportError:
        _log.warning("firebase-admin not installed — FCM push skipped. Install: pip install firebase-admin")
        return False
    except Exception as exc:
        _log.error("FCM push failed: %s", exc)
        return False


# ── Public API ────────────────────────────────────────────────────────────────

async def dispatch(tokens: PushTokens, msg_id: str, from_did: str) -> None:
    """
    Dispatch a wakeup push to all available device tokens.
    Runs APNs and FCM concurrently. Errors are logged, not raised.
    """
    tasks = []
    if tokens.get("apns"):
        tasks.append(send_apns(tokens["apns"], msg_id, from_did))
    if tokens.get("fcm"):
        tasks.append(send_fcm(tokens["fcm"], msg_id, from_did))
    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        _log.debug("Push dispatch results for %s: %s", msg_id, results)
