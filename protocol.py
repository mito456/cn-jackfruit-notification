"""
protocol.py
-----------
Binary wire-format definitions for the UDP data channel.

UDP Packet Layout
=================

  [HEADER 6 bytes, plaintext]  [NONCE 12 bytes]  [CIPHERTEXT + GCM-TAG 16 bytes]
   |-version-|-type-|--seq--|   |---random----|   |----AES-256-GCM encrypted----|

  • HEADER is passed as Associated Authenticated Data (AAD) to AES-GCM, so any
    tampering with the header is detected by the GCM tag even though the header
    itself is not encrypted.
  • NONCE is randomly generated per packet (prevents replay if seq is replayed).
  • CIPHERTEXT carries the message payload (can be empty for ACK/heartbeat).
  • GCM TAG (16 bytes, appended by the cryptography library) provides integrity
    and authenticity.

TCP Control Channel
===================
Length-prefixed (4-byte big-endian) JSON messages over a TLS socket.

  subscribe    →  {"type": "subscribe",   "client_name": str, "udp_port": int}
  sub_ack      ←  {"type": "sub_ack",     "session_key": b64str, "server_udp_port": int}
  unsubscribe  →  {"type": "unsubscribe"}
  error        ←  {"type": "error",       "message": str}
"""

import struct
from typing import Tuple

# ── Protocol version ────────────────────────────────────────────────────────
VERSION: int = 1

# ── UDP message types ────────────────────────────────────────────────────────
MSG_NOTIFICATION:   int = 0x01   # Server → Client  (broadcast)
MSG_ACK:            int = 0x02   # Client → Server  (reliable delivery)
MSG_HEARTBEAT:      int = 0x03   # Server → Client  (liveness probe)
MSG_HEARTBEAT_ACK:  int = 0x04   # Client → Server  (liveness reply)

# ── TCP control message type strings ────────────────────────────────────────
CTRL_SUBSCRIBE:   str = "subscribe"
CTRL_SUB_ACK:     str = "sub_ack"
CTRL_UNSUBSCRIBE: str = "unsubscribe"
CTRL_ERROR:       str = "error"

# ── Header layout: Version(1B) | Type(1B) | SeqNum(4B) = 6 bytes ────────────
_HEADER_FORMAT = "!BBI"
HEADER_SIZE: int = struct.calcsize(_HEADER_FORMAT)   # 6

# ── Reliability tuning ────────────────────────────────────────────────────────
MAX_RETRIES:          int   = 5      # max retransmit attempts per client
RETRANSMIT_TIMEOUT:   float = 2.0   # seconds before first/next retransmit
CHECK_INTERVAL:       float = 0.5   # how often the retransmit thread wakes up
HEARTBEAT_INTERVAL:   float = 10.0  # seconds between heartbeat probes
HEARTBEAT_DEAD_LIMIT: float = 35.0  # seconds of silence → client presumed dead
MAX_UDP_BUFFER:       int   = 65535


# ── Header helpers ────────────────────────────────────────────────────────────

def pack_header(msg_type: int, seq_num: int) -> bytes:
    """Return the 6-byte binary header."""
    return struct.pack(_HEADER_FORMAT, VERSION, msg_type, seq_num)


def unpack_header(data: bytes) -> Tuple[int, int, int]:
    """Extract (version, msg_type, seq_num) from the first 6 bytes."""
    if len(data) < HEADER_SIZE:
        raise ValueError(f"Data too short for header ({len(data)} < {HEADER_SIZE})")
    return struct.unpack(_HEADER_FORMAT, data[:HEADER_SIZE])


# ── Full-packet helpers (uses security module) ────────────────────────────────

def build_udp_packet(msg_type: int, seq_num: int, key: bytes,
                     payload: bytes = b"") -> bytes:
    """
    Construct an authenticated-encrypted UDP packet.

    Layout: HEADER(6) || NONCE(12) || CIPHERTEXT+TAG(variable)
    The HEADER is used as AAD so it is integrity-protected but visible.
    """
    from security import encrypt_message   # late import avoids circular issue
    header = pack_header(msg_type, seq_num)
    encrypted_body = encrypt_message(key, payload, associated_data=header)
    return header + encrypted_body


def parse_udp_packet(data: bytes, key: bytes) -> Tuple[int, int, bytes]:
    """
    Decode and authenticate a UDP packet.

    Returns (msg_type, seq_num, plaintext_payload).
    Raises ValueError / InvalidTag on bad data or authentication failure.
    """
    from security import decrypt_message
    header = data[:HEADER_SIZE]
    _version, msg_type, seq_num = unpack_header(header)
    encrypted_body = data[HEADER_SIZE:]
    payload = decrypt_message(key, encrypted_body, associated_data=header)
    return msg_type, seq_num, payload
