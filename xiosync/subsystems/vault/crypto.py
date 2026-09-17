"""Vault crypto layer — AES-256-GCM symmetric encryption.

Per-org keys derived via HMAC-SHA256(master_secret, org_id).
Platform-level secrets (org_id=None) use HMAC(master_secret, b"__platform__").

No external KMS dependency — the master secret lives in XIOSYNC_AUTH_SECRET
(already required for JWT signing).  The per-org key derivation means:
  - Compromising one org's key does not expose others.
  - Rotating the master rotates all derived keys simultaneously.
  - A platform admin can decrypt platform-global secrets; org admins
    can only decrypt their own org's secrets.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import struct
import uuid
from dataclasses import dataclass

# ── AES-256-GCM via stdlib (Python 3.8+ cryptography or fallback) ─────────────
try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _HAS_CRYPTOGRAPHY = True
except ImportError:  # pragma: no cover
    _HAS_CRYPTOGRAPHY = False

__all__ = ["VaultCrypto", "VaultCryptoError"]

_PLATFORM_SCOPE = b"__platform__"
_KEY_LEN = 32   # AES-256
_IV_LEN  = 12   # GCM nonce
_TAG_LEN = 16   # GCM auth tag


class VaultCryptoError(Exception):
    """Raised on decryption failure (bad key, tampered ciphertext)."""


class VaultCrypto:
    """Stateless AES-256-GCM wrapper with per-org key derivation."""

    def __init__(self, master_secret: str) -> None:
        """
        Args:
            master_secret: The XIOSYNC_AUTH_SECRET value.
        """
        self._master = master_secret.encode()

    # ── Key derivation ────────────────────────────────────────────────────────

    def _derive_key(self, org_id: uuid.UUID | None) -> bytes:
        """Derive a 256-bit AES key scoped to an org (or the platform)."""
        scope = str(org_id).encode() if org_id else _PLATFORM_SCOPE
        return hmac.new(self._master, scope, hashlib.sha256).digest()  # 32 bytes

    # ── Encrypt / Decrypt ─────────────────────────────────────────────────────

    def encrypt(
        self,
        plaintext: str,
        org_id: uuid.UUID | None,
    ) -> tuple[bytes, bytes, bytes]:
        """Encrypt *plaintext* for *org_id*.

        Returns:
            (ciphertext, iv, auth_tag) — all bytes, store in DB columns.
        """
        if not _HAS_CRYPTOGRAPHY:
            raise VaultCryptoError(
                "cryptography package required: uv add cryptography"
            )
        key = self._derive_key(org_id)
        iv = os.urandom(_IV_LEN)
        aesgcm = AESGCM(key)
        # AESGCM.encrypt returns ciphertext + 16-byte tag concatenated
        ct_and_tag = aesgcm.encrypt(iv, plaintext.encode(), None)
        ciphertext = ct_and_tag[:-_TAG_LEN]
        auth_tag   = ct_and_tag[-_TAG_LEN:]
        return ciphertext, iv, auth_tag

    def decrypt(
        self,
        ciphertext: bytes,
        iv: bytes,
        auth_tag: bytes,
        org_id: uuid.UUID | None,
    ) -> str:
        """Decrypt a vaulted secret.

        Raises:
            VaultCryptoError: If the key is wrong or data has been tampered with.
        """
        if not _HAS_CRYPTOGRAPHY:
            raise VaultCryptoError(
                "cryptography package required: uv add cryptography"
            )
        key = self._derive_key(org_id)
        aesgcm = AESGCM(key)
        try:
            plaintext = aesgcm.decrypt(iv, ciphertext + auth_tag, None)
        except Exception as exc:
            raise VaultCryptoError("Decryption failed — wrong key or tampered data") from exc
        return plaintext.decode()
