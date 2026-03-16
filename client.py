"""
client.py – Jackfruit Notification Client
==========================================

Subscription flow
-----------------
  1. Client binds a UDP socket on an auto-assigned (or specified) port.
  2. Client opens a TLS connection to the server's TCP control port.
  3. Client sends:  {"type": "subscribe", "client_name": <name>, "udp_port": <port>}
  4. Server responds: {"type": "sub_ack", "session_key": <b64>, "server_udp_port": <port>}
  5. Client stores the AES-256 session key and server's UDP port.

Receiving notifications
-----------------------
  A background thread listens on the client's UDP socket.
  For each incoming datagram:
    • Decrypt using the session key (AES-256-GCM).
    • Authenticate via GCM tag (InvalidTag → drop & warn).
    • Check sequence number for duplicates (set-based rolling window).
    • Print notification if new.
    • Send MSG_ACK back to the server's UDP port (always, even for duplicates).

Heartbeat handling
------------------
  The server sends MSG_HEARTBEAT periodically.
  Client replies with MSG_HEARTBEAT_ACK so the server knows it is alive.

Reliability properties (client side)
-------------------------------------
  • Duplicate detection   – sequence numbers in a rolling set
  • ACK for duplicates    – re-send ACK; server's original ACK may have been lost
  • Graceful disconnect   – sends UNSUBSCRIBE over TLS before closing
"""

import base64
import json
import logging
import socket
import ssl
import sys
import threading
import time
from typing import Optional

from protocol import (
    CTRL_SUB_ACK, CTRL_SUBSCRIBE, CTRL_UNSUBSCRIBE,
    MAX_UDP_BUFFER, MSG_ACK, MSG_HEARTBEAT, MSG_HEARTBEAT_ACK,
    MSG_NOTIFICATION, build_udp_packet, parse_udp_packet,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)-8s]  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)


class NotificationClient:
    def __init__(
        self,
        client_name:   str,
        server_host:   str = "localhost",
        server_tcp_port: int = 9000,
        udp_listen_port: int = 0,       # 0 = OS picks a free port
        cafile:        str = "server.crt",
    ) -> None:
        self.client_name      = client_name
        self.server_host      = server_host
        self.server_tcp_port  = server_tcp_port
        self.udp_listen_port  = udp_listen_port
        self.cafile           = cafile

        self.log = logging.getLogger(f"Client[{client_name}]")

        # Set after successful subscription
        self.session_key:    Optional[bytes] = None
        self.server_udp_port: Optional[int]  = None

        # Sockets
        self.tcp_conn:  Optional[ssl.SSLSocket]  = None
        self.udp_sock:  Optional[socket.socket]  = None

        # Duplicate detection: set of recently seen sequence numbers
        self._seen_seqs:      set   = set()
        self._seen_lock       = threading.Lock()

        # Stats
        self.stat_received:   int = 0
        self.stat_duplicates: int = 0
        self.stat_acks_sent:  int = 0

        self.running = False

        # TLS context (client side) – verifies server certificate
        self.ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.ssl_ctx.load_verify_locations(cafile)
        # check_hostname can be True for production; False simplifies
        # multi-machine testing when connecting by IP
        self.ssl_ctx.check_hostname  = False
        self.ssl_ctx.verify_mode     = ssl.CERT_REQUIRED

    # ── Connection & subscription ─────────────────────────────────────────

    def connect(self) -> "NotificationClient":
        """
        1. Bind UDP socket (learn actual port).
        2. Open TLS connection to server.
        3. Send SUBSCRIBE, receive session key.
        """
        # Bind UDP first so we know which port to advertise
        self.udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp_sock.bind(("", self.udp_listen_port))
        self.udp_sock.settimeout(1.0)
        actual_udp_port = self.udp_sock.getsockname()[1]
        self.log.info("UDP listener bound on port %d", actual_udp_port)

        # TLS handshake
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw.connect((self.server_host, self.server_tcp_port))
        self.tcp_conn = self.ssl_ctx.wrap_socket(
            raw, server_hostname=self.server_host
        )
        self.log.info(
            "TLS connected  %s:%d  |  %s  cipher=%s",
            self.server_host, self.server_tcp_port,
            self.tcp_conn.version(),
            self.tcp_conn.cipher()[0],
        )

        # Send SUBSCRIBE
        self._tcp_send({
            "type":        CTRL_SUBSCRIBE,
            "client_name": self.client_name,
            "udp_port":    actual_udp_port,
        })

        # Receive SUB_ACK with session key
        resp = self._tcp_recv()
        if resp is None or resp.get("type") != CTRL_SUB_ACK:
            raise ConnectionError(f"Subscription failed: {resp}")

        self.session_key     = base64.b64decode(resp["session_key"])
        self.server_udp_port = int(resp["server_udp_port"])
        self.log.info(
            "Subscribed – session key %d bytes, server UDP port %d",
            len(self.session_key), self.server_udp_port,
        )
        return self

    def start(self) -> None:
        """Start UDP listener thread, then block (main thread) until quit."""
        if self.session_key is None:
            raise RuntimeError("Call connect() before start()")
        self.running = True
        t = threading.Thread(target=self._udp_recv_loop,
                             name="UDP-Recv", daemon=True)
        t.start()

        print()
        print("=" * 60)
        print(f"  Client '{self.client_name}' – subscribed & listening")
        print("  Press Ctrl-C to disconnect.")
        print("=" * 60)
        print()

        try:
            while self.running:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            self.disconnect()

    # ── Disconnect ────────────────────────────────────────────────────────

    def disconnect(self) -> None:
        self.log.info("Disconnecting …")
        self.running = False
        # Graceful unsubscribe over TLS
        if self.tcp_conn:
            try:
                self._tcp_send({"type": CTRL_UNSUBSCRIBE})
            except OSError:
                pass
            try:
                self.tcp_conn.close()
            except OSError:
                pass
        if self.udp_sock:
            try:
                self.udp_sock.close()
            except OSError:
                pass
        self.log.info(
            "Final stats – received=%d, duplicates=%d, acks_sent=%d",
            self.stat_received, self.stat_duplicates, self.stat_acks_sent,
        )

    # ── UDP receive loop ──────────────────────────────────────────────────

    def _udp_recv_loop(self) -> None:
        while self.running:
            try:
                data, addr = self.udp_sock.recvfrom(MAX_UDP_BUFFER)
            except socket.timeout:
                continue
            except OSError:
                break
            self._dispatch_udp(data, addr)

    def _dispatch_udp(self, data: bytes, addr: tuple) -> None:
        if not self.session_key:
            return
        try:
            msg_type, seq_num, payload = parse_udp_packet(data, self.session_key)
        except Exception as exc:
            self.log.warning("Decrypt/auth failed from %s: %s", addr, exc)
            return

        if msg_type == MSG_NOTIFICATION:
            self._handle_notification(seq_num, payload, addr)
        elif msg_type == MSG_HEARTBEAT:
            self._send_heartbeat_ack(seq_num, addr)
        else:
            self.log.debug("Unknown UDP type 0x%02x from %s", msg_type, addr)

    # ── Notification handling ─────────────────────────────────────────────

    def _handle_notification(self, seq_num: int,
                              payload: bytes, addr: tuple) -> None:
        # Always ACK — even for duplicates (our previous ACK may have been lost,
        # causing the server to retransmit)
        self._send_ack(seq_num, addr)

        with self._seen_lock:
            duplicate = seq_num in self._seen_seqs
            if not duplicate:
                self._seen_seqs.add(seq_num)
                # Bound memory: keep only the last 1 000 sequence numbers
                if len(self._seen_seqs) > 1000:
                    cutoff = max(self._seen_seqs) - 500
                    self._seen_seqs = {s for s in self._seen_seqs if s > cutoff}

        if duplicate:
            self.stat_duplicates += 1
            self.log.debug("Duplicate notification #%d – ACK re-sent", seq_num)
            return

        self.stat_received += 1
        message = payload.decode("utf-8", errors="replace")

        # Pretty-print the notification
        ts = time.strftime("%H:%M:%S")
        print(f"\n  ┌{'─'*54}┐")
        print(f"  │  [{ts}]  NOTIFICATION  #{seq_num:<6}              │")
        print(f"  │  {message[:52]:<52}  │")
        if len(message) > 52:
            for i in range(52, len(message), 52):
                print(f"  │  {message[i:i+52]:<52}  │")
        print(f"  └{'─'*54}┘")

    # ── ACK / Heartbeat-ACK senders ───────────────────────────────────────

    def _send_ack(self, seq_num: int, server_addr: tuple) -> None:
        dst = (server_addr[0], self.server_udp_port)
        pkt = build_udp_packet(MSG_ACK, seq_num, self.session_key, b"")
        try:
            self.udp_sock.sendto(pkt, dst)
            self.stat_acks_sent += 1
            self.log.debug("ACK #%d → %s", seq_num, dst)
        except OSError as exc:
            self.log.error("Failed to send ACK #%d: %s", seq_num, exc)

    def _send_heartbeat_ack(self, seq_num: int, server_addr: tuple) -> None:
        dst = (server_addr[0], self.server_udp_port)
        pkt = build_udp_packet(MSG_HEARTBEAT_ACK, seq_num, self.session_key, b"")
        try:
            self.udp_sock.sendto(pkt, dst)
            self.log.debug("Heartbeat-ACK → %s", dst)
        except OSError:
            pass

    # ── TCP helpers ───────────────────────────────────────────────────────

    def _tcp_send(self, obj: dict) -> None:
        data = json.dumps(obj).encode("utf-8")
        self.tcp_conn.sendall(len(data).to_bytes(4, "big") + data)

    def _tcp_recv(self) -> Optional[dict]:
        try:
            raw = b""
            while len(raw) < 4:
                chunk = self.tcp_conn.recv(4 - len(raw))
                if not chunk:
                    return None
                raw += chunk
            length = int.from_bytes(raw, "big")
            if length > 65535:
                return None
            buf = b""
            while len(buf) < length:
                chunk = self.tcp_conn.recv(length - len(buf))
                if not chunk:
                    return None
                buf += chunk
            return json.loads(buf.decode("utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return None


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    import argparse

    p = argparse.ArgumentParser(description="Jackfruit Notification Client")
    p.add_argument("name",       help="Unique client display name")
    p.add_argument("--host",     default="localhost",   help="Server hostname/IP")
    p.add_argument("--tcp-port", default=9000, type=int, help="Server TLS control port")
    p.add_argument("--udp-port", default=0,    type=int, help="Local UDP port (0=auto)")
    p.add_argument("--cafile",   default="server.crt",  help="Server CA certificate")
    args = p.parse_args()

    client = NotificationClient(
        client_name=args.name,
        server_host=args.host,
        server_tcp_port=args.tcp_port,
        udp_listen_port=args.udp_port,
        cafile=args.cafile,
    )
    try:
        client.connect()
        client.start()
    except KeyboardInterrupt:
        client.disconnect()
    except Exception as exc:
        logging.getLogger("Client").error("Fatal: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
