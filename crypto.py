"""AES-256-GCM envelope encryption for the zero-knowledge image vault.

Design / threat model
---------------------
* Every image is encrypted with its own fresh 256-bit data key (DEK) using
  AES-256-GCM.  The nonce is 12 random bytes per encryption.
* DEKs are *wrapped* (encrypted) with a key-encryption key (KEK) derived from
  the MASTER_PASSPHRASE with Argon2id (memory-hard).  The Argon2 salt lives in
  the database, the passphrase never does -> a stolen vault.db alone is
  useless: without the passphrase the wrapped DEKs cannot be unwrapped.
* Every ciphertext is bound to its image id via AES-GCM AAD, so ciphertext
  blobs cannot be swapped between vault rows without detection.
* Plaintext exists only in RAM, is passed around as ``bytearray`` and is
  best-effort zeroised after use.  (Python cannot strictly guarantee absence
  of transient interpreter copies - see README "Trust model" for the honest
  wording.)
* For zero-knowledge web links the DEK is exported once, placed in the URL
  #fragment (never transmitted to the server) and wiped from RAM.
"""
from __future__ import annotations

import base64
import os

from argon2.low_level import Type as ArgonType, hash_secret_raw
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_LEN = 32   # AES-256
NONCE_LEN = 12 # standard GCM nonce size

# Argon2id parameters: ~64 MiB, 3 passes.  Tune via env if too slow on tiny VPS.
ARGON2_TIME_COST = 3
ARGON2_MEMORY_COST = 64 * 1024
ARGON2_PARALLELISM = 2


class CryptoError(Exception):
    """Raised when unwrapping or decrypting fails (wrong passphrase / tamper)."""


def new_salt() -> bytes:
    return os.urandom(16)


def derive_kek(passphrase: str, salt: bytes) -> bytes:
    """Derive the 256-bit key-encryption key from the master passphrase."""
    return hash_secret_raw(
        secret=passphrase.encode("utf-8"),
        salt=salt,
        time_cost=ARGON2_TIME_COST,
        memory_cost=ARGON2_MEMORY_COST,
        parallelism=ARGON2_PARALLELISM,
        hash_len=KEY_LEN,
        type=ArgonType.ID,
    )


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def wipe(buf) -> None:
    """Best-effort in-place zeroisation of a mutable buffer."""
    if isinstance(buf, bytearray):
        for i in range(len(buf)):
            buf[i] = 0


class VaultCrypto:
    """Encrypts images under per-image DEKs wrapped by the master KEK."""

    def __init__(self, passphrase: str, salt: bytes):
        if not passphrase:
            raise CryptoError("MASTER_PASSPHRASE is empty")
        self._kek = derive_kek(passphrase, salt)

    # -- encrypt -------------------------------------------------------------
    def encrypt(self, image_id: str, plaintext: bytes) -> dict:
        """Encrypt plaintext for ``image_id``.  Returns wrapped key material.

        The caller is responsible for wiping ``plaintext`` afterwards.
        """
        dek = bytearray(os.urandom(KEY_LEN))
        nonce = os.urandom(NONCE_LEN)
        aad = image_id.encode("ascii")
        try:
            ct = AESGCM(dek).encrypt(nonce, plaintext, aad)
            wrap_nonce = os.urandom(NONCE_LEN)
            wrapped_key = AESGCM(self._kek).encrypt(wrap_nonce, bytes(dek), aad)
        finally:
            wipe(dek)
        return {
            "nonce": nonce,
            "ct": ct,
            "wrap_nonce": wrap_nonce,
            "wrapped_key": wrapped_key,
        }

    # -- decrypt -------------------------------------------------------------
    def decrypt(self, image_id: str, nonce: bytes, ct: bytes,
                wrap_nonce: bytes, wrapped_key: bytes) -> bytearray:
        """Unwrap the DEK and decrypt the image.  Caller MUST wipe the result."""
        dek = self.unwrap_key(image_id, wrap_nonce, wrapped_key)
        try:
            pt = AESGCM(dek).decrypt(nonce, ct, image_id.encode("ascii"))
        except InvalidTag as exc:
            raise CryptoError("ciphertext failed authentication (tampered?)") from exc
        finally:
            wipe(dek)
        return bytearray(pt)

    def unwrap_key(self, image_id: str, wrap_nonce: bytes,
                   wrapped_key: bytes) -> bytearray:
        """Recover the raw per-image DEK (for building zero-knowledge links).

        The caller MUST wipe the returned buffer after use.
        """
        try:
            dek = bytearray(AESGCM(self._kek).decrypt(
                wrap_nonce, wrapped_key, image_id.encode("ascii")))
        except InvalidTag as exc:
            raise CryptoError(
                "key unwrap failed - wrong MASTER_PASSPHRASE or corrupted vault"
            ) from exc
        return dek
