"""
server.py – Jackfruit Group Notification Server
================================================

Architecture
------------
  Two-socket design for clean separation of concerns:

  TCP/TLS  port 9000  – Control channel (subscription management, key exchange)
  UDP      port 9001  – Data channel   (encrypted group notifications)

Thread model
------------
  Main thread      : admin console (type messages to broadcast)
  TCP-Accept       : accepts new TLS connections from clients
  Client-<addr>    : one thread per connected client (handles subscribe/unsubscribe)
  UDP-Recv         : single thread receives all incoming UDP (ACKs, heartbeats)
  Retransmit       : periodically retransmits unacknowledged notifications
  Heartbeat        : periodically probes subscribed clients for liveness

Reliability mechanisms (UDP)
----------------------------
  1. Sequence numbers  – monotonically increasing counter, per-broadcast
  2. ACK from client   – client sends MSG_ACK{seq} for every notification
  3. Retransmission    – server re-sends after RETRANSMIT_TIMEOUT seconds
  4. Max retries       – after MAX_RETRIES failures the client is evicted
  5. Duplicate guard   – client-side; server reacts to duplicate ACKs safely

Security
--------
  Control channel : TLS 1.2+ (standard SSL socket) – prevents MITM on key exchange
  Data channel    : AES-256-GCM per-client session key – confidentiality +
                    integrity + authenticity of every UDP datagram
"""

import base64
import json
import logging
import os
import socket
import ssl
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Set

from protocol import (
    CTRL_ERROR, CTRL_SUB_ACK, CTRL_SUBSCRIBE, CTRL_UNSUBSCRIBE,
    CHECK_INTERVAL, HEADER_SIZE, HEARTBEAT_DEAD_LIMIT, HEARTBEAT_INTERVAL,
    MAX_RETRIES, MAX_UDP_BUFFER, MSG_ACK, MSG_HEARTBEAT,
    MSG_HEARTBEAT_ACK, MSG_NOTIFICATION, RETRANSMIT_TIMEOUT,
    build_udp_packet, parse_udp_packet,
)
from security import generate_session_key

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)-8s]  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("Server")


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class ClientInfo:
    """All state the server stores per subscribed client."""
    name:           str
    udp_addr:       tuple           # (ip, udp_port) used to send notifications
    session_key:    bytes           # unique AES-256 key per client
    tcp_conn:       object          # live TLS socket (for unsubscribe detection)
    last_heartbeat: float = field(default_factory=time.time)
    # stats
    notifications_sent:  int = 0
    notifications_acked: int = 0


@dataclass
class PendingMessage:
    """Tracks outstanding ACKs for one broadcast notification."""
    seq_num:         int
    message_text:    str
    # per-client encrypted bytes (different key ⇒ different ciphertext)
    encrypted_data:  Dict[tuple, bytes] = field(default_factory=dict)
    # addresses that have NOT yet acknowledged
    pending_clients: Set[tuple]         = field(default_factory=set)
    # retry bookkeeping per client
    retries:         Dict[tuple, int]   = field(default_factory=dict)
    last_sent:       Dict[tuple, float] = field(default_factory=dict)
    created_at:      float              = field(default_factory=time.time)


# ── Server ────────────────────────────────────────────────────────────────────

class NotificationServer:
    def __init__(
        self,
        tcp_host: str  = "0.0.0.0",
        tcp_port: int  = 9000,
        udp_port: int  = 9001,
        certfile: str  = "server.crt",
        keyfile:  str  = "server.key",
    ) -> None:
        self.tcp_host = tcp_host
        self.tcp_port = tcp_port
        self.udp_port = udp_port

        # ── Client registry ──────────────────────────────────────────────
        # Key: (client_ip, client_udp_port)
        self.clients:      Dict[tuple, ClientInfo]     = {}
        self.clients_lock: threading.Lock              = threading.Lock()

        # ── Pending ACK table ────────────────────────────────────────────
        # Key: seq_num
        self.pending_acks: Dict[int, PendingMessage]   = {}
        self.pending_lock: threading.Lock              = threading.Lock()

        # ── Sequence counter ─────────────────────────────────────────────
        self._seq: int           = 0
        self._seq_lock           = threading.Lock()

        # ── Aggregate stats ──────────────────────────────────────────────
        self.stat_broadcast_count:    int = 0
        self.stat_acks_received:      int = 0
        self.stat_retransmit_count:   int = 0

        # ── TLS context ──────────────────────────────────────────────────
        self.ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ssl_ctx.load_cert_chain(certfile, keyfile)
        self.ssl_ctx.minimum_version = ssl.TLSVersion.TLSv1_2

        # ── Sockets ──────────────────────────────────────────────────────
        self.tcp_sock: Optional[socket.socket] = None
        self.udp_sock: Optional[socket.socket] = None
        self.running = False

    # ── Public API ────────────────────────────────────────────────────────

    def start(self) -> None:
        """Bind sockets, launch threads, enter admin console."""
        self.running = True

        # TCP/TLS control socket
        self.tcp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.tcp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.tcp_sock.bind((self.tcp_host, self.tcp_port))
        self.tcp_sock.listen(20)
        log.info("TCP/TLS control  → %s:%d", self.tcp_host, self.tcp_port)

        # UDP data socket
        self.udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp_sock.bind((self.tcp_host, self.udp_port))
        self.udp_sock.settimeout(1.0)
        log.info("UDP notification → %s:%d", self.tcp_host, self.udp_port)

        # Background threads (all daemon so they die with main thread)
        self._spawn("TCP-Accept",  self._accept_loop)
        self._spawn("UDP-Recv",    self._udp_recv_loop)
        self._spawn("Retransmit",  self._retransmit_loop)
        self._spawn("Heartbeat",   self._heartbeat_loop)

        print()
        print("=" * 60)
        print("  Jackfruit Notification Server  –  READY")
        print("=" * 60)
        print("  Commands:")
        print("    <message>  – broadcast notification to all clients")
        print("    list       – show subscribed clients")
        print("    stats      – show performance statistics")
        print("    quit       – shutdown server")
        print("=" * 60)
        print()

        self._admin_console()

    def stop(self) -> None:
        log.info("Shutting down …")
        self.running = False
        if self.tcp_sock:
            try: self.tcp_sock.close()
            except OSError: pass
        if self.udp_sock:
            try: self.udp_sock.close()
            except OSError: pass

    # ── Admin console ─────────────────────────────────────────────────────

    def _admin_console(self) -> None:
        while self.running:
            try:
                line = input()
            except (EOFError, KeyboardInterrupt):
                self.stop()
                break
            cmd = line.strip()
            if not cmd:
                continue
            if cmd.lower() == "quit":
                self.stop()
                break
            elif cmd.lower() == "list":
                self._print_clients()
            elif cmd.lower() == "stats":
                self._print_stats()
            else:
                self.broadcast(cmd)

    def _print_clients(self) -> None:
        with self.clients_lock:
            snap = list(self.clients.values())
        if not snap:
            print("  (no clients subscribed)")
            return
        print(f"\n{'─'*56}")
        print(f"  {'Name':<20} {'UDP Address':<22} {'Sent':>5} {'ACKd':>5}")
        print(f"{'─'*56}")
        for c in snap:
            addr_str = f"{c.udp_addr[0]}:{c.udp_addr[1]}"
            print(f"  {c.name:<20} {addr_str:<22} {c.notifications_sent:>5} {c.notifications_acked:>5}")
        print(f"{'─'*56}\n")

    def _print_stats(self) -> None:
        with self.pending_lock:
            pending = sum(len(p.pending_clients) for p in self.pending_acks.values())
        with self.clients_lock:
            client_count = len(self.clients)
        print(f"\n  Broadcasts sent     : {self.stat_broadcast_count}")
        print(f"  ACKs received       : {self.stat_acks_received}")
        print(f"  Retransmissions     : {self.stat_retransmit_count}")
        print(f"  Pending ACKs now    : {pending}")
        print(f"  Subscribed clients  : {client_count}\n")

    # ── Broadcast ─────────────────────────────────────────────────────────

    def broadcast(self, message: str) -> None:
        """Encrypt and send a notification to every subscribed client."""
        seq = self._next_seq()
        self.stat_broadcast_count += 1
        payload = message.encode("utf-8")

        with self.clients_lock:
            clients_snap = dict(self.clients)

        if not clients_snap:
            print("  [!] No clients subscribed – message not sent.")
            return

        pm = PendingMessage(seq_num=seq, message_text=message)
        now = time.time()

        for udp_addr, ci in clients_snap.items():
            pkt = build_udp_packet(MSG_NOTIFICATION, seq, ci.session_key, payload)
            pm.encrypted_data[udp_addr] = pkt
            pm.pending_clients.add(udp_addr)
            pm.retries[udp_addr]   = 0
            pm.last_sent[udp_addr] = now

        # Register before sending to avoid a race where ACK arrives
        # before pending_acks entry exists
        with self.pending_lock:
            self.pending_acks[seq] = pm

        # Transmit
        for udp_addr, ci in clients_snap.items():
            try:
                self.udp_sock.sendto(pm.encrypted_data[udp_addr], udp_addr)
                ci.notifications_sent += 1
            except OSError as exc:
                log.error("Send failed → %s: %s", udp_addr, exc)

        short = message if len(message) <= 60 else message[:57] + "..."
        log.info("Broadcast #%d to %d client(s): '%s'",
                 seq, len(clients_snap), short)

    # ── TCP accept loop ────────────────────────────────────────────────────

    def _accept_loop(self) -> None:
        while self.running:
            try:
                raw, addr = self.tcp_sock.accept()
            except OSError:
                break  # socket closed
            try:
                tls = self.ssl_ctx.wrap_socket(raw, server_side=True)
            except ssl.SSLError as exc:
                log.warning("TLS handshake failed from %s: %s", addr, exc)
                raw.close()
                continue
            self._spawn(f"Client-{addr}", self._handle_client_tcp,
                        args=(tls, addr))

    # ── TCP per-client handler ─────────────────────────────────────────────

    def _handle_client_tcp(self, conn: ssl.SSLSocket, addr: tuple) -> None:
        ci: Optional[ClientInfo] = None
        try:
            # ── Receive SUBSCRIBE ────────────────────────────────────────
            msg = self._tcp_recv(conn)
            if msg is None or msg.get("type") != CTRL_SUBSCRIBE:
                self._tcp_send(conn, {"type": CTRL_ERROR,
                                      "message": "Expected subscribe"})
                return

            name     = str(msg.get("client_name", "unnamed"))[:64]
            udp_port = msg.get("udp_port")
            if not isinstance(udp_port, int) or not (1024 <= udp_port <= 65535):
                self._tcp_send(conn, {"type": CTRL_ERROR,
                                      "message": "Invalid udp_port"})
                return

            udp_addr    = (addr[0], udp_port)
            session_key = generate_session_key()
            ci          = ClientInfo(name=name, udp_addr=udp_addr,
                                     session_key=session_key, tcp_conn=conn)

            with self.clients_lock:
                self.clients[udp_addr] = ci

            # ── Send SUB_ACK + session key over TLS ──────────────────────
            self._tcp_send(conn, {
                "type":            CTRL_SUB_ACK,
                "session_key":     base64.b64encode(session_key).decode(),
                "server_udp_port": self.udp_port,
            })

            tls_ver = conn.version()
            cipher  = conn.cipher()
            log.info("SUBSCRIBE '%s' from %s  |  TLS=%s  cipher=%s",
                     name, udp_addr, tls_ver, cipher[0] if cipher else "?")

            # ── Stay alive until client unsubscribes or disconnects ───────
            while self.running:
                msg = self._tcp_recv(conn)
                if msg is None:          # connection closed
                    break
                if msg.get("type") == CTRL_UNSUBSCRIBE:
                    log.info("UNSUBSCRIBE '%s'", name)
                    break

        except Exception as exc:
            log.debug("Client handler error %s: %s", addr, exc)
        finally:
            if ci is not None:
                with self.clients_lock:
                    self.clients.pop(ci.udp_addr, None)
                log.info("Client '%s' disconnected", ci.name)
            try:
                conn.close()
            except OSError:
                pass

    # ── UDP receive loop ───────────────────────────────────────────────────

    def _udp_recv_loop(self) -> None:
        while self.running:
            try:
                data, src_addr = self.udp_sock.recvfrom(MAX_UDP_BUFFER)
            except socket.timeout:
                continue
            except OSError:
                break
            self._dispatch_udp(data, src_addr)

    def _dispatch_udp(self, data: bytes, src_addr: tuple) -> None:
        """Identify the sender, decrypt, and handle the UDP packet."""
        # Look up client by source (ip, port)
        # Must not hold clients_lock while decrypting (avoid long critical section)
        with self.clients_lock:
            ci = self.clients.get(src_addr)
        if ci is None:
            log.debug("UDP from unknown address %s – ignored", src_addr)
            return

        try:
            msg_type, seq_num, _payload = parse_udp_packet(data, ci.session_key)
        except Exception as exc:
            log.warning("Decrypt failed from '%s': %s", ci.name, exc)
            return

        if msg_type == MSG_ACK:
            self._handle_ack(src_addr, seq_num, ci)
        elif msg_type == MSG_HEARTBEAT_ACK:
            ci.last_heartbeat = time.time()
            log.debug("Heartbeat-ACK from '%s'", ci.name)
        else:
            log.debug("Unknown UDP type 0x%02x from '%s'", msg_type, ci.name)

    def _handle_ack(self, udp_addr: tuple, seq_num: int,
                    ci: ClientInfo) -> None:
        self.stat_acks_received += 1
        ci.notifications_acked += 1
        with self.pending_lock:
            pm = self.pending_acks.get(seq_num)
            if pm is None:
                return  # already fully ACKed or evicted
            pm.pending_clients.discard(udp_addr)
            log.debug("ACK #%d from '%s' (%d remaining)",
                      seq_num, ci.name, len(pm.pending_clients))
            if not pm.pending_clients:
                del self.pending_acks[seq_num]
                log.info("Notification #%d fully acknowledged (all clients ACKed)",
                         seq_num)

    # ── Retransmit thread ─────────────────────────────────────────────────

    def _retransmit_loop(self) -> None:
        """
        Periodically scan pending_acks and retransmit to any client that
        has not yet ACKed within RETRANSMIT_TIMEOUT seconds.
        Clients that exceed MAX_RETRIES are evicted from the subscriber list.
        """
        while self.running:
            time.sleep(CHECK_INTERVAL)
            now = time.time()
            evict      : list   = []   # clients to remove after lock release
            seqs_done  : list   = []   # fully resolved seq_nums

            with self.pending_lock:
                for seq_num, pm in list(self.pending_acks.items()):
                    for udp_addr in list(pm.pending_clients):
                        elapsed = now - pm.last_sent.get(udp_addr, pm.created_at)
                        if elapsed < RETRANSMIT_TIMEOUT:
                            continue

                        retries = pm.retries.get(udp_addr, 0)
                        if retries >= MAX_RETRIES:
                            log.warning(
                                "Max retries (%d) reached for #%d → %s. Evicting.",
                                MAX_RETRIES, seq_num, udp_addr,
                            )
                            pm.pending_clients.discard(udp_addr)
                            evict.append(udp_addr)
                        else:
                            pkt = pm.encrypted_data.get(udp_addr)
                            if pkt:
                                try:
                                    self.udp_sock.sendto(pkt, udp_addr)
                                    self.stat_retransmit_count += 1
                                    pm.retries[udp_addr]   = retries + 1
                                    pm.last_sent[udp_addr] = now
                                    log.debug(
                                        "Retransmit #%d/attempt-%d → %s",
                                        seq_num, retries + 1, udp_addr,
                                    )
                                except OSError:
                                    pm.pending_clients.discard(udp_addr)
                                    evict.append(udp_addr)

                    if not pm.pending_clients:
                        seqs_done.append(seq_num)

                for s in seqs_done:
                    self.pending_acks.pop(s, None)

            # Evict unresponsive clients (separate lock acquisition)
            if evict:
                with self.clients_lock:
                    for udp_addr in evict:
                        gone = self.clients.pop(udp_addr, None)
                        if gone:
                            log.info("Evicted unresponsive client '%s'", gone.name)

    # ── Heartbeat thread ──────────────────────────────────────────────────

    def _heartbeat_loop(self) -> None:
        """
        Send a lightweight MSG_HEARTBEAT every HEARTBEAT_INTERVAL seconds.
        Remove clients that have not replied within HEARTBEAT_DEAD_LIMIT seconds.
        """
        while self.running:
            time.sleep(HEARTBEAT_INTERVAL)
            now = time.time()
            dead: list = []

            with self.clients_lock:
                snap = dict(self.clients)

            for udp_addr, ci in snap.items():
                if now - ci.last_heartbeat > HEARTBEAT_DEAD_LIMIT:
                    dead.append(udp_addr)
                    continue
                seq = self._next_seq()
                pkt = build_udp_packet(MSG_HEARTBEAT, seq, ci.session_key, b"")
                try:
                    self.udp_sock.sendto(pkt, udp_addr)
                except OSError:
                    dead.append(udp_addr)

            if dead:
                with self.clients_lock:
                    for udp_addr in dead:
                        gone = self.clients.pop(udp_addr, None)
                        if gone:
                            log.info("Heartbeat timeout – evicted '%s'", gone.name)

    # ── Helpers ───────────────────────────────────────────────────────────

    def _next_seq(self) -> int:
        with self._seq_lock:
            self._seq += 1
            return self._seq

    @staticmethod
    def _tcp_send(conn: ssl.SSLSocket, obj: dict) -> None:
        data = json.dumps(obj).encode("utf-8")
        conn.sendall(len(data).to_bytes(4, "big") + data)

    @staticmethod
    def _tcp_recv(conn: ssl.SSLSocket) -> Optional[dict]:
        try:
            raw = b""
            while len(raw) < 4:
                chunk = conn.recv(4 - len(raw))
                if not chunk:
                    return None
                raw += chunk
            length = int.from_bytes(raw, "big")
            if length > 65535:
                return None
            buf = b""
            while len(buf) < length:
                chunk = conn.recv(length - len(buf))
                if not chunk:
                    return None
                buf += chunk
            return json.loads(buf.decode("utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return None

    def _spawn(self, name: str, target, args: tuple = ()) -> threading.Thread:
        t = threading.Thread(target=target, args=args, name=name, daemon=True)
        t.start()
        return t


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    import argparse

    p = argparse.ArgumentParser(description="Jackfruit Notification Server")
    p.add_argument("--host",     default="0.0.0.0",    help="Bind address")
    p.add_argument("--tcp-port", default=9000,  type=int, help="TLS control port")
    p.add_argument("--udp-port", default=9001,  type=int, help="UDP data port")
    p.add_argument("--cert",     default="server.crt", help="TLS certificate")
    p.add_argument("--key",      default="server.key", help="TLS private key")
    args = p.parse_args()

    if not os.path.exists(args.cert) or not os.path.exists(args.key):
        print("[ERROR] Certificate files not found. Run:  python generate_certs.py")
        sys.exit(1)

    server = NotificationServer(
        tcp_host=args.host,
        tcp_port=args.tcp_port,
        udp_port=args.udp_port,
        certfile=args.cert,
        keyfile=args.key,
    )
    server.start()


if __name__ == "__main__":
    main()
