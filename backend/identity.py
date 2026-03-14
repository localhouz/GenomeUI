"""
Genome Identity — persistent per-user cryptographic identity.

On first run, generates a random 32-byte seed, derives an ed25519 keypair,
and stores the seed in the OS keychain (Windows Credential Manager /
macOS Keychain / Linux Secret Service — same mechanism as OAuth tokens).

On subsequent runs, loads the seed from the keychain so the identity is
stable across restarts.

Public surface:
    identity = load()       # GenomedIdentity namedtuple
    identity.did            # "did:key:z..." — shareable on the mesh
    identity.public_key_hex # hex-encoded 32-byte public key
    identity.seed_hex       # hex-encoded 32-byte seed (for libp2p peer ID)
    identity.recovery_phrase  # 24-word BIP39 mnemonic (write this down)

Recovery (new device, no existing Genome device nearby):
    identity = restore(phrase)   # returns GenomedIdentity or None
"""

from __future__ import annotations

import logging
import secrets
from typing import NamedTuple

import base64
import hashlib

import keyring
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, PrivateFormat, NoEncryption

_log = logging.getLogger(__name__)

_KEYCHAIN_SERVICE = "genome-identity"
_KEYCHAIN_SEED_KEY = "seed"

# Multicodec varint prefix for ed25519 public keys (used in did:key)
_ED25519_MULTICODEC = b"\xed\x01"

# Base58btc alphabet (Bitcoin)
_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

# Curve25519 prime — used for ed25519→X25519 public key conversion
_P25519 = 2**255 - 19


def _public_key_bytes_from_did(did: str) -> bytes | None:
    """Extract raw 32-byte ed25519 public key from a did:key DID."""
    if not did.startswith("did:key:z"):
        return None
    try:
        encoded = did[len("did:key:z"):]
        n = 0
        for ch in encoded:
            idx = _BASE58_ALPHABET.find(ch)
            if idx < 0:
                return None
            n = n * 58 + idx
        length = max(1, (n.bit_length() + 7) // 8)
        decoded = n.to_bytes(length, "big")
        extra = sum(1 for ch in encoded if ch == _BASE58_ALPHABET[0])
        decoded = b"\x00" * extra + decoded
        if len(decoded) != 34 or decoded[0] != 0xED or decoded[1] != 0x01:
            return None
        return decoded[2:]
    except Exception:  # noqa: BLE001
        return None


def _ed25519_seed_to_x25519_scalar(seed: bytes) -> bytes:
    """Derive X25519 private scalar from ed25519 seed (SHA-512 + RFC 7748 clamp)."""
    h = bytearray(hashlib.sha512(seed).digest()[:32])
    h[0] &= 248
    h[31] &= 127
    h[31] |= 64
    return bytes(h)


def _ed25519_pub_to_x25519_pub(pub: bytes) -> bytes:
    """Convert ed25519 public key bytes to X25519 Montgomery-u coordinate."""
    y_bytes = bytearray(pub)
    y_bytes[31] &= 0x7F  # clear sign bit of x
    y = int.from_bytes(y_bytes, "little")
    denom = (1 - y) % _P25519
    u = (1 + y) * pow(denom, _P25519 - 2, _P25519) % _P25519
    return u.to_bytes(32, "little")


class GenomedIdentity(NamedTuple):
    """Persistent per-user cryptographic identity derived from an ed25519 keypair."""

    did: str              # did:key:z<base58btc(0xed01 + pubkey)>
    public_key_hex: str   # hex-encoded 32-byte ed25519 public key
    seed_hex: str         # hex-encoded 32-byte seed (deterministic peer ID)
    recovery_phrase: str  # 24-word BIP39 mnemonic

    def sign(self, message: bytes) -> str:
        """Sign arbitrary bytes with this identity's private key.

        Returns a hex-encoded ed25519 signature suitable for use in
        a GenomeEnvelope ``sig`` field.
        """
        private_key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(self.seed_hex))
        raw_sig = private_key.sign(message)
        return raw_sig.hex()

    def ecdh_encrypt(self, their_did: str, plaintext: bytes) -> str | None:
        """ECDH-encrypt plaintext for a recipient identified by their DID.

        Derives an X25519 keypair from our ed25519 seed, performs ECDH with
        the recipient's X25519 public key (derived from their did:key), and
        encrypts with AES-256-GCM + HKDF.

        Returns base64(nonce[12] + ciphertext), or None if the DID is invalid.
        """
        their_pub = _public_key_bytes_from_did(their_did)
        if not their_pub:
            return None
        try:
            their_x = _ed25519_pub_to_x25519_pub(their_pub)
            scalar = _ed25519_seed_to_x25519_scalar(bytes.fromhex(self.seed_hex))
            shared = X25519PrivateKey.from_private_bytes(scalar).exchange(
                X25519PublicKey.from_public_bytes(their_x)
            )
            aes_key = HKDF(
                algorithm=hashes.SHA256(), length=32, salt=None, info=b"genome-p2p-v1"
            ).derive(shared)
            nonce = secrets.token_bytes(12)
            ct = AESGCM(aes_key).encrypt(nonce, plaintext, None)
            return base64.b64encode(nonce + ct).decode()
        except Exception:  # noqa: BLE001
            return None

    def ecdh_decrypt(self, their_did: str, ciphertext_b64: str) -> bytes | None:
        """Decrypt a p2p ciphertext produced by the holder of their_did."""
        their_pub = _public_key_bytes_from_did(their_did)
        if not their_pub:
            return None
        try:
            their_x = _ed25519_pub_to_x25519_pub(their_pub)
            scalar = _ed25519_seed_to_x25519_scalar(bytes.fromhex(self.seed_hex))
            shared = X25519PrivateKey.from_private_bytes(scalar).exchange(
                X25519PublicKey.from_public_bytes(their_x)
            )
            aes_key = HKDF(
                algorithm=hashes.SHA256(), length=32, salt=None, info=b"genome-p2p-v1"
            ).derive(shared)
            data = base64.b64decode(ciphertext_b64)
            return AESGCM(aes_key).decrypt(data[:12], data[12:], None)
        except Exception:  # noqa: BLE001
            return None

    def group_encrypt(self, group_key_hex: str, plaintext: bytes) -> str:
        """Symmetric encrypt with a shared group key (AES-256-GCM + HKDF)."""
        try:
            aes_key = HKDF(
                algorithm=hashes.SHA256(), length=32, salt=None, info=b"genome-group-v1"
            ).derive(bytes.fromhex(group_key_hex))
            nonce = secrets.token_bytes(12)
            ct = AESGCM(aes_key).encrypt(nonce, plaintext, None)
            return base64.b64encode(nonce + ct).decode()
        except Exception:  # noqa: BLE001
            return ""

    def group_decrypt(self, group_key_hex: str, ciphertext_b64: str) -> bytes | None:
        """Decrypt a group-encrypted ciphertext."""
        try:
            aes_key = HKDF(
                algorithm=hashes.SHA256(), length=32, salt=None, info=b"genome-group-v1"
            ).derive(bytes.fromhex(group_key_hex))
            data = base64.b64decode(ciphertext_b64)
            return AESGCM(aes_key).decrypt(data[:12], data[12:], None)
        except Exception:  # noqa: BLE001
            return None


# ── Internal ──────────────────────────────────────────────────────────────────

def _base58btc(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    result: list[str] = []
    while n:
        n, r = divmod(n, 58)
        result.append(_BASE58_ALPHABET[r])
    for b in data:
        if b == 0:
            result.append(_BASE58_ALPHABET[0])
        else:
            break
    return "".join(reversed(result))


def _did_from_pubkey(public_key_bytes: bytes) -> str:
    return "did:key:z" + _base58btc(_ED25519_MULTICODEC + public_key_bytes)


def _recovery_phrase(seed: bytes) -> str:
    try:
        from mnemonic import Mnemonic  # type: ignore[import]
        return Mnemonic("english").to_mnemonic(seed)
    except Exception:
        _log.warning("'mnemonic' package unavailable — recovery phrase is raw hex")
        return seed.hex()


def _build(seed: bytes) -> GenomedIdentity:
    private_key = Ed25519PrivateKey.from_private_bytes(seed)
    pub_bytes = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return GenomedIdentity(
        did=_did_from_pubkey(pub_bytes),
        public_key_hex=pub_bytes.hex(),
        seed_hex=seed.hex(),
        recovery_phrase=_recovery_phrase(seed),
    )


# ── Public API ────────────────────────────────────────────────────────────────

def load() -> GenomedIdentity:
    """
    Load the persistent Genome identity from the OS keychain, or generate
    a new one if this is the first run. Call once at backend startup.
    """
    stored = keyring.get_password(_KEYCHAIN_SERVICE, _KEYCHAIN_SEED_KEY)
    if stored:
        try:
            seed = bytes.fromhex(stored)
            if len(seed) == 32:
                _log.info("Genome identity loaded from OS keychain")
                return _build(seed)
        except Exception:
            pass
        _log.warning("Genome identity in keychain is corrupt — regenerating")

    seed = secrets.token_bytes(32)
    try:
        keyring.set_password(_KEYCHAIN_SERVICE, _KEYCHAIN_SEED_KEY, seed.hex())
        _log.info("Generated new Genome identity and saved to OS keychain")
    except Exception as exc:
        _log.warning("Could not save identity to keychain: %s — identity will not persist across restarts", exc)

    return _build(seed)


def restore(phrase: str) -> GenomedIdentity | None:
    """
    Restore a Genome identity from a 24-word BIP39 recovery phrase.
    Saves the restored seed to the OS keychain.
    Returns None if the phrase is invalid.
    """
    try:
        from mnemonic import Mnemonic  # type: ignore[import]
        m = Mnemonic("english")
        if not m.check(phrase.strip()):
            _log.warning("Recovery phrase checksum invalid")
            return None
        seed = bytes(m.to_entropy(phrase.strip()))
        if len(seed) != 32:
            _log.warning("Recovery phrase produced wrong seed length: %d", len(seed))
            return None
        keyring.set_password(_KEYCHAIN_SERVICE, _KEYCHAIN_SEED_KEY, seed.hex())
        _log.info("Genome identity restored from recovery phrase")
        return _build(seed)
    except Exception as exc:
        _log.warning("Failed to restore from recovery phrase: %s", exc)
        return None
