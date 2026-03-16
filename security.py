"""
security.py
-----------
Application-layer encryption for the UDP data channel.

Mechanism: AES-256-GCM  (AEAD – Authenticated Encryption with Associated Data)
  • Confidentiality  – payload is encrypted with AES-256
  • Integrity        – GCM authentication tag (16 bytes) detects any modification
  • Authenticity     – only the holder of the shared session key can produce a
                       valid tag, so messages are implicitly authenticated
  • Replay defence   – each packet uses a freshly generated 96-bit random nonce;
                       combined with the sequence number in the AAD, duplicate /
                       replayed packets are detected at the protocol layer

Wire format produced by encrypt_message
========================================
  NONCE (12 bytes)  ||  CIPHERTEXT + TAG (variable)

The caller is responsible for prepending the AAD (packet header) to the
datagram – the AAD itself is NOT included in the returned bytes.

Session key exchange
====================
Each client receives a unique 256-bit (32-byte) AES key over the TLS control
channel during subscription.  Keys are generated with os.urandom, which uses
the OS CSPRNG (e.g., /dev/urandom on Linux, CryptGenRandom on Windows).
"""

import os
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag

NONCE_SIZE: int = 12   # bytes – standard for AES-GCM (96-bit nonce)
TAG_SIZE:   int = 16   # bytes – GCM auth tag appended by the library
KEY_SIZE:   int = 32   # bytes – AES-256


def generate_session_key() -> bytes:
    """Return a cryptographically random 256-bit AES session key."""
    return os.urandom(KEY_SIZE)


def encrypt_message(key: bytes, plaintext: bytes,
                    associated_data: bytes = b"") -> bytes:
    """
    Encrypt *plaintext* with AES-256-GCM.

    Parameters
    ----------
    key             : 32-byte session key
    plaintext       : raw payload bytes (may be empty for ACK packets)
    associated_data : authenticated but NOT encrypted (e.g. packet header)

    Returns
    -------
    nonce (12 B) || ciphertext || tag (16 B)
    """
    aesgcm = AESGCM(key)
    nonce = os.urandom(NONCE_SIZE)
    # AESGCM.encrypt returns ciphertext || tag
    ciphertext_tag = aesgcm.encrypt(nonce, plaintext,
                                    associated_data or None)
    return nonce + ciphertext_tag


def decrypt_message(key: bytes, data: bytes,
                    associated_data: bytes = b"") -> bytes:
    """
    Decrypt and authenticate an AES-256-GCM message.

    Parameters
    ----------
    key             : 32-byte session key
    data            : nonce (12 B) || ciphertext || tag (16 B)
    associated_data : same bytes that were passed during encryption

    Returns
    -------
    plaintext bytes

    Raises
    ------
    ValueError   – data is too short to be a valid encrypted message
    InvalidTag   – authentication failed (tampered, wrong key, or replay)
    """
    min_len = NONCE_SIZE + TAG_SIZE   # 28 bytes minimum (empty plaintext)
    if len(data) < min_len:
        raise ValueError(
            f"Encrypted data too short: {len(data)} < {min_len} bytes"
        )
    aesgcm = AESGCM(key)
    nonce = data[:NONCE_SIZE]
    ciphertext_tag = data[NONCE_SIZE:]
    # Raises InvalidTag automatically if authentication fails
    return aesgcm.decrypt(nonce, ciphertext_tag, associated_data or None)
