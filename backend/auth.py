"""
auth.py — GenomeUI local authentication and OAuth token vault.

Security model:
  • One physical user per machine — no multi-user login.
  • Passkey (WebAuthn / FIDO2) gates all access. The private key lives in
    the device TPM/Secure Enclave; biometric or PIN is required to sign.
  • OAuth tokens are stored in the OS credential store (Windows Credential
    Manager / macOS Keychain / Linux Secret Service) via the `keyring`
    library. Tokens never touch a database or the filesystem in plaintext.
    The OS enforces user-boundary ACLs on top of hardware-backed encryption.
  • SQLite stores only passkey *public* key data — not a secret.
  • Sessions are short-lived in-memory UUIDs issued after a successful
    passkey assertion. They die when the backend restarts and are never
    written to disk.

Environment variables:
  GENOME_AUTH_ENABLED   true | false (default true — secure by default)
  GENOME_AUTH_ORIGIN    WebAuthn origin (default http://localhost:5173)
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import pathlib
import secrets
import sqlite3
import time
from typing import Any

import webauthn
from webauthn import base64url_to_bytes, options_to_json
from webauthn.helpers.structs import (
    AuthenticationCredential,
    AuthenticatorAssertionResponse,
    AuthenticatorAttestationResponse,
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    PublicKeyCredentialType,
    RegistrationCredential,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)
from webauthn.helpers.cose import COSEAlgorithmIdentifier

import keyring
import keyring.errors
import httpx

# ── Configuration ──────────────────────────────────────────────────────────────

AUTH_ENABLED = os.getenv("GENOME_AUTH_ENABLED", "true").strip().lower() == "true"
RP_ID        = "localhost"
RP_NAME      = "GenomeUI"
ORIGIN       = os.getenv("GENOME_AUTH_ORIGIN", "http://localhost:5173")

_USER_ID           = b"genome-local-user"
_USER_NAME         = "local"
_USER_DISPLAY_NAME = "GenomeUI User"

SESSION_TTL      = 8 * 60 * 60   # 8 hours
_CHALLENGE_TTL   = 300           # 5 minutes

DATA_DIR         = pathlib.Path(__file__).parent / "data"
VAULT_DB         = DATA_DIR / "auth_vault.db"
_VAULT_INDEX     = DATA_DIR / "vault_index.json"   # service names only — not secrets
_KEYRING_SERVICE = "GenomeUI"

# ── Database ───────────────────────────────────────────────────────────────────

def _db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(VAULT_DB))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # Passkey public key data is not a secret — SQLite is appropriate here.
    # OAuth tokens live in the OS keychain (keyring), never here.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS passkey (
            credential_id   BLOB PRIMARY KEY,
            public_key      BLOB NOT NULL,
            sign_count      INTEGER NOT NULL DEFAULT 0,
            created_at      INTEGER NOT NULL
        )
    """)
    conn.commit()
    return conn

# ── Session store (in-memory only) ────────────────────────────────────────────

# token → expiry unix timestamp
_sessions: dict[str, float] = {}

def session_create() -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = time.time() + SESSION_TTL
    return token

def session_valid(token: str | None) -> bool:
    """Return True if token is a live session. Always True when auth disabled."""
    if not AUTH_ENABLED:
        return True
    if not token:
        return False
    exp = _sessions.get(token)
    if exp is None:
        return False
    if time.time() > exp:
        _sessions.pop(token, None)
        return False
    return True

def session_revoke(token: str) -> None:
    _sessions.pop(token, None)

def _prune_sessions() -> None:
    now = time.time()
    dead = [t for t, exp in _sessions.items() if now > exp]
    for t in dead:
        del _sessions[t]

# ── Pending WebAuthn challenges ────────────────────────────────────────────────

# nonce → (challenge_bytes, expires_at)
_reg_challenges:  dict[str, tuple[bytes, float]] = {}
_auth_challenges: dict[str, tuple[bytes, float]] = {}

def _prune_challenges(store: dict[str, tuple[bytes, float]]) -> None:
    now = time.time()
    dead = [k for k, (_, exp) in store.items() if now > exp]
    for k in dead:
        del store[k]

# ── Passkey status ─────────────────────────────────────────────────────────────

def passkey_registered() -> bool:
    conn = _db()
    row  = conn.execute("SELECT COUNT(*) FROM passkey").fetchone()
    conn.close()
    return row[0] > 0

# ── Registration ───────────────────────────────────────────────────────────────

def registration_begin() -> dict[str, Any]:
    _prune_challenges(_reg_challenges)
    options = webauthn.generate_registration_options(
        rp_id=RP_ID,
        rp_name=RP_NAME,
        user_id=_USER_ID,
        user_name=_USER_NAME,
        user_display_name=_USER_DISPLAY_NAME,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        supported_pub_key_algs=[
            COSEAlgorithmIdentifier.ECDSA_SHA_256,
            COSEAlgorithmIdentifier.RSASSA_PKCS1_v1_5_SHA_256,
        ],
    )
    nonce = secrets.token_urlsafe(16)
    _reg_challenges[nonce] = (options.challenge, time.time() + _CHALLENGE_TTL)
    result = json.loads(options_to_json(options))
    result["nonce"] = nonce
    return result

def registration_complete(nonce: str, credential_body: dict[str, Any]) -> str:
    """Verify registration response. Returns a new session token."""
    entry = _reg_challenges.pop(nonce, None)
    if entry is None or time.time() > entry[1]:
        raise ValueError("Unknown or expired registration challenge")
    challenge = entry[0]

    credential = RegistrationCredential(
        id=credential_body["id"],
        raw_id=_b64url(credential_body["rawId"]),
        response=AuthenticatorAttestationResponse(
            client_data_json=_b64url(credential_body["response"]["clientDataJSON"]),
            attestation_object=_b64url(credential_body["response"]["attestationObject"]),
        ),
    )
    verified = webauthn.verify_registration_response(
        credential=credential,
        expected_challenge=challenge,
        expected_rp_id=RP_ID,
        expected_origin=ORIGIN,
        require_user_verification=True,
    )
    conn = _db()
    conn.execute(
        "INSERT OR REPLACE INTO passkey (credential_id, public_key, sign_count, created_at) VALUES (?,?,?,?)",
        (verified.credential_id, verified.credential_public_key, verified.sign_count, int(time.time())),
    )
    conn.commit()
    conn.close()
    return session_create()

# ── Authentication ─────────────────────────────────────────────────────────────

def authentication_begin() -> dict[str, Any]:
    _prune_challenges(_auth_challenges)
    conn = _db()
    rows = conn.execute("SELECT credential_id FROM passkey").fetchall()
    conn.close()
    allow = [
        PublicKeyCredentialDescriptor(
            type=PublicKeyCredentialType.PUBLIC_KEY,
            id=bytes(row["credential_id"]),
        )
        for row in rows
    ]
    options = webauthn.generate_authentication_options(
        rp_id=RP_ID,
        allow_credentials=allow,
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    nonce = secrets.token_urlsafe(16)
    _auth_challenges[nonce] = (options.challenge, time.time() + _CHALLENGE_TTL)
    result = json.loads(options_to_json(options))
    result["nonce"] = nonce
    return result

def authentication_complete(nonce: str, assertion_body: dict[str, Any]) -> str:
    """Verify passkey assertion. Returns a new session token."""
    entry = _auth_challenges.pop(nonce, None)
    if entry is None or time.time() > entry[1]:
        raise ValueError("Unknown or expired authentication challenge")
    challenge = entry[0]

    resp = assertion_body["response"]
    user_handle = _b64url(resp["userHandle"]) if resp.get("userHandle") else None
    assertion = AuthenticationCredential(
        id=assertion_body["id"],
        raw_id=_b64url(assertion_body["rawId"]),
        response=AuthenticatorAssertionResponse(
            client_data_json=_b64url(resp["clientDataJSON"]),
            authenticator_data=_b64url(resp["authenticatorData"]),
            signature=_b64url(resp["signature"]),
            user_handle=user_handle,
        ),
    )
    conn = _db()
    row = conn.execute(
        "SELECT public_key, sign_count FROM passkey WHERE credential_id = ?",
        (assertion.raw_id,),
    ).fetchone()
    if row is None:
        conn.close()
        raise ValueError("Credential not registered")

    verified = webauthn.verify_authentication_response(
        credential=assertion,
        expected_challenge=challenge,
        expected_rp_id=RP_ID,
        expected_origin=ORIGIN,
        credential_public_key=bytes(row["public_key"]),
        credential_current_sign_count=row["sign_count"],
        require_user_verification=True,
    )
    conn.execute(
        "UPDATE passkey SET sign_count = ? WHERE credential_id = ?",
        (verified.new_sign_count, assertion.raw_id),
    )
    conn.commit()
    conn.close()
    return session_create()

# ── OAuth Token Vault (OS keychain via keyring) ────────────────────────────────
# Tokens live in Windows Credential Manager / macOS Keychain / Linux Secret
# Service. The OS enforces user-boundary ACLs; tokens never touch a file or DB.
# We keep a plain JSON index of *service names only* (not secrets) so vault_list
# works — keyring has no enumerate API.

def _index_read() -> list[str]:
    try:
        return json.loads(_VAULT_INDEX.read_text()) if _VAULT_INDEX.exists() else []
    except Exception:
        return []

def _index_write(services: list[str]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _VAULT_INDEX.write_text(json.dumps(sorted(set(services))))

def vault_store(service: str, token_data: dict[str, Any]) -> None:
    keyring.set_password(_KEYRING_SERVICE, service, json.dumps(token_data))
    _index_write(_index_read() + [service])

def vault_retrieve(service: str) -> dict[str, Any] | None:
    raw = keyring.get_password(_KEYRING_SERVICE, service)
    return json.loads(raw) if raw is not None else None

def vault_delete(service: str) -> None:
    try:
        keyring.delete_password(_KEYRING_SERVICE, service)
    except keyring.errors.PasswordDeleteError:
        pass
    _index_write([s for s in _index_read() if s != service])

def vault_list() -> list[str]:
    return _index_read()

# ── Utilities ──────────────────────────────────────────────────────────────────

class ConnectorAuthError(RuntimeError):
    """Raised when a connector token is missing, expired, or can no longer refresh."""

    def __init__(self, service: str, message: str | None = None) -> None:
        super().__init__(message or f"Connector '{service}' requires re-authentication.")
        self.service = service


_REFRESH_CONFIGS: dict[str, dict[str, str | bool]] = {
    "gmail": {
        "token_url": "https://oauth2.googleapis.com/token",
        "client_id_env": "GMAIL_CLIENT_ID",
        "client_secret_env": "GMAIL_CLIENT_SECRET",
        "include_client_secret": True,
    },
    "gcal": {
        "token_url": "https://oauth2.googleapis.com/token",
        "client_id_env": "GCAL_CLIENT_ID",
        "client_secret_env": "GCAL_CLIENT_SECRET",
        "include_client_secret": True,
    },
    "spotify": {
        "token_url": "https://accounts.spotify.com/api/token",
        "client_id_env": "SPOTIFY_CLIENT_ID",
        "client_secret_env": "SPOTIFY_CLIENT_SECRET",
        "include_client_secret": False,
    },
}


def _refresh_config_for(service: str) -> dict[str, str | bool] | None:
    config = _REFRESH_CONFIGS.get(str(service or "").strip().lower())
    if not config:
        return None
    return {
        "token_url": str(config["token_url"]),
        "client_id": str(os.getenv(str(config["client_id_env"]), "")).strip(),
        "client_secret": str(os.getenv(str(config["client_secret_env"]), "")).strip(),
        "include_client_secret": bool(config.get("include_client_secret", True)),
    }


def refresh_token_if_needed_sync(service: str) -> dict[str, Any]:
    """
    Return a valid vault token, refreshing it if expiry is within 60 seconds.

    Raises ConnectorAuthError when the connector is missing, revoked, or cannot
    be refreshed into a usable state.
    """
    service_norm = str(service or "").strip().lower()
    tok = vault_retrieve(service_norm)
    if not tok:
        raise ConnectorAuthError(service_norm)

    access_token = str(tok.get("access_token", "") or "").strip()
    expires_at = float(tok.get("expires_at", 0) or 0)
    if access_token and expires_at and time.time() <= expires_at - 60:
        return tok

    config = _refresh_config_for(service_norm)
    if not config:
        if access_token:
            return tok
        raise ConnectorAuthError(service_norm)

    refresh_token = str(tok.get("refresh_token", "") or "").strip()
    client_id = str(config.get("client_id", "") or "").strip()
    client_secret = str(config.get("client_secret", "") or "").strip()
    if not refresh_token or not client_id:
        raise ConnectorAuthError(service_norm)

    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }
    if bool(config.get("include_client_secret", True)) and client_secret:
        data["client_secret"] = client_secret

    try:
        resp = httpx.post(str(config["token_url"]), data=data, timeout=10)
    except Exception as exc:
        raise ConnectorAuthError(service_norm, f"Failed to refresh {service_norm}: {exc}") from exc

    if resp.status_code != 200:
        raise ConnectorAuthError(service_norm, f"{service_norm} refresh failed: HTTP {resp.status_code}")

    payload = resp.json()
    next_access = str(payload.get("access_token", "") or "").strip()
    if not next_access:
        raise ConnectorAuthError(service_norm, f"{service_norm} refresh returned no access token")

    tok["access_token"] = next_access
    tok["expires_at"] = time.time() + float(payload.get("expires_in", 3600) or 3600)
    if payload.get("refresh_token"):
        tok["refresh_token"] = str(payload.get("refresh_token") or "").strip()
    vault_store(service_norm, tok)
    return tok


async def refresh_token_if_needed(service: str) -> dict[str, Any]:
    """Async helper for connector code paths that want a ready-to-use OAuth token record."""
    return await asyncio.to_thread(refresh_token_if_needed_sync, service)


def _b64url(s: str) -> bytes:
    """Decode a base64url string to bytes, tolerating missing padding."""
    s = s.replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    return base64.b64decode(s)
