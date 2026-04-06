"""
jackfruit_gui.py – Jackfruit Notification System GUI
=====================================================

A single-file tkinter GUI that wraps both the server and client from the
cn-jackfruit-notification project.

HOW TO RUN
----------
1. Place this file in the same directory as:
       server.py, client.py, protocol.py, security.py,
       server.crt, server.key

2. Install dependencies (if not already):
       pip install cryptography

3. Run:
       python jackfruit_gui.py

The GUI has two tabs:
  • Server  – start/stop the server and broadcast messages
  • Client  – connect as a named subscriber and view incoming notifications
"""

import base64
import json
import logging
import os
import queue
import socket
import ssl
import sys
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox, scrolledtext, ttk
from typing import Optional

# ── Try importing project modules (must be in same directory) ─────────────────
try:
    from protocol import (
        CTRL_SUB_ACK, CTRL_SUBSCRIBE, CTRL_UNSUBSCRIBE,
        MAX_UDP_BUFFER, MSG_ACK, MSG_HEARTBEAT, MSG_HEARTBEAT_ACK,
        MSG_NOTIFICATION, build_udp_packet, parse_udp_packet,
    )
    from security import generate_session_key
except ImportError as _e:
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror(
        "Missing project files",
        f"Could not import project modules:\n{_e}\n\n"
        "Make sure jackfruit_gui.py is in the same folder as\n"
        "protocol.py, security.py, server.py, client.py."
    )
    sys.exit(1)

# ── Colour palette ────────────────────────────────────────────────────────────
BG        = "#1e1e2e"
BG2       = "#2a2a3e"
BG3       = "#313145"
ACCENT    = "#cba6f7"   # lavender
GREEN     = "#a6e3a1"
RED       = "#f38ba8"
YELLOW    = "#f9e2af"
TEXT      = "#cdd6f4"
SUBTEXT   = "#a6adc8"
BORDER    = "#45475a"

# ─────────────────────────────────────────────────────────────────────────────
# Minimal embedded server / client (no import of server.py / client.py so we
# can capture all output to the GUI queues instead of stdout).
# ─────────────────────────────────────────────────────────────────────────────

# ── Embedded server ────────────────────────────────────────────────────────────

from dataclasses import dataclass, field
from typing import Dict, Set

@dataclass
class _ClientInfo:
    name:           str
    udp_addr:       tuple
    session_key:    bytes
    tcp_conn:       object
    last_heartbeat: float = field(default_factory=time.time)
    notifications_sent:  int = 0
    notifications_acked: int = 0

@dataclass
class _PendingMessage:
    seq_num:         int
    message_text:    str
    encrypted_data:  Dict[tuple, bytes] = field(default_factory=dict)
    pending_clients: Set[tuple]         = field(default_factory=set)
    retries:         Dict[tuple, int]   = field(default_factory=dict)
    last_sent:       Dict[tuple, float] = field(default_factory=dict)
    created_at:      float              = field(default_factory=time.time)

# protocol constants
from protocol import (
    CTRL_ERROR, CTRL_SUB_ACK, CTRL_SUBSCRIBE, CTRL_UNSUBSCRIBE,
    CHECK_INTERVAL, HEARTBEAT_DEAD_LIMIT, HEARTBEAT_INTERVAL,
    MAX_RETRIES, RETRANSMIT_TIMEOUT,
)


class EmbeddedServer:
    """
    A version of NotificationServer that writes log lines to a queue
    instead of stdout, and exposes a broadcast() method callable from the GUI.
    """

    def __init__(self, log_q: queue.Queue, clients_q: queue.Queue,
                 tcp_host="0.0.0.0", tcp_port=9000, udp_port=9001,
                 certfile="server.crt", keyfile="server.key"):
        self._log_q     = log_q
        self._clients_q = clients_q
        self.tcp_host   = tcp_host
        self.tcp_port   = tcp_port
        self.udp_port   = udp_port

        self.clients:      Dict[tuple, _ClientInfo]    = {}
        self.clients_lock  = threading.Lock()
        self.pending_acks: Dict[int, _PendingMessage]  = {}
        self.pending_lock  = threading.Lock()

        self._seq      = 0
        self._seq_lock = threading.Lock()

        self.stat_broadcast_count  = 0
        self.stat_acks_received    = 0
        self.stat_retransmit_count = 0

        self.ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ssl_ctx.load_cert_chain(certfile, keyfile)
        self.ssl_ctx.minimum_version = ssl.TLSVersion.TLSv1_2

        self.tcp_sock: Optional[socket.socket] = None
        self.udp_sock: Optional[socket.socket] = None
        self.running = False

    def _log(self, level: str, msg: str):
        ts = time.strftime("%H:%M:%S")
        self._log_q.put((level, f"[{ts}]  {msg}"))
        self._push_clients()

    def _push_clients(self):
        with self.clients_lock:
            snap = [(c.name, f"{c.udp_addr[0]}:{c.udp_addr[1]}",
                     c.notifications_sent, c.notifications_acked)
                    for c in self.clients.values()]
        self._clients_q.put(snap)

    def start(self):
        self.running = True
        self.tcp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.tcp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.tcp_sock.bind((self.tcp_host, self.tcp_port))
        self.tcp_sock.listen(20)

        self.udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp_sock.bind((self.tcp_host, self.udp_port))
        self.udp_sock.settimeout(1.0)

        self._log("INFO", f"TCP/TLS control  → {self.tcp_host}:{self.tcp_port}")
        self._log("INFO", f"UDP notification → {self.tcp_host}:{self.udp_port}")
        self._log("INFO", "Server READY – waiting for clients …")

        for name, target in [
            ("TCP-Accept", self._accept_loop),
            ("UDP-Recv",   self._udp_recv_loop),
            ("Retransmit", self._retransmit_loop),
            ("Heartbeat",  self._heartbeat_loop),
        ]:
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()

    def stop(self):
        self._log("INFO", "Shutting down …")
        self.running = False
        for s in (self.tcp_sock, self.udp_sock):
            if s:
                try: s.close()
                except OSError: pass

    def broadcast(self, message: str):
        seq = self._next_seq()
        self.stat_broadcast_count += 1
        payload = message.encode("utf-8")

        with self.clients_lock:
            snap = dict(self.clients)

        if not snap:
            self._log("WARN", "No clients subscribed – message not sent.")
            return

        pm = _PendingMessage(seq_num=seq, message_text=message)
        now = time.time()
        for udp_addr, ci in snap.items():
            pkt = build_udp_packet(MSG_NOTIFICATION, seq, ci.session_key, payload)
            pm.encrypted_data[udp_addr]  = pkt
            pm.pending_clients.add(udp_addr)
            pm.retries[udp_addr]         = 0
            pm.last_sent[udp_addr]       = now

        with self.pending_lock:
            self.pending_acks[seq] = pm

        for udp_addr, ci in snap.items():
            try:
                self.udp_sock.sendto(pm.encrypted_data[udp_addr], udp_addr)
                ci.notifications_sent += 1
            except OSError as exc:
                self._log("ERROR", f"Send failed → {udp_addr}: {exc}")

        short = message if len(message) <= 60 else message[:57] + "…"
        self._log("INFO", f"Broadcast #{seq} to {len(snap)} client(s): '{short}'")

    # ── internal threads ──────────────────────────────────────────────────

    def _accept_loop(self):
        while self.running:
            try:
                raw, addr = self.tcp_sock.accept()
            except OSError:
                break
            try:
                tls = self.ssl_ctx.wrap_socket(raw, server_side=True)
            except ssl.SSLError as exc:
                self._log("WARN", f"TLS handshake failed from {addr}: {exc}")
                raw.close()
                continue
            t = threading.Thread(target=self._handle_client_tcp,
                                 args=(tls, addr), daemon=True)
            t.start()

    def _handle_client_tcp(self, conn, addr):
        ci = None
        try:
            msg = self._tcp_recv(conn)
            if msg is None or msg.get("type") != CTRL_SUBSCRIBE:
                self._tcp_send(conn, {"type": CTRL_ERROR, "message": "Expected subscribe"})
                return

            name     = str(msg.get("client_name", "unnamed"))[:64]
            udp_port = msg.get("udp_port")
            if not isinstance(udp_port, int) or not (1024 <= udp_port <= 65535):
                self._tcp_send(conn, {"type": CTRL_ERROR, "message": "Invalid udp_port"})
                return

            udp_addr    = (addr[0], udp_port)
            session_key = generate_session_key()
            ci          = _ClientInfo(name=name, udp_addr=udp_addr,
                                      session_key=session_key, tcp_conn=conn)

            with self.clients_lock:
                self.clients[udp_addr] = ci

            self._tcp_send(conn, {
                "type":            CTRL_SUB_ACK,
                "session_key":     base64.b64encode(session_key).decode(),
                "server_udp_port": self.udp_port,
            })
            self._log("INFO", f"SUBSCRIBE '{name}' from {udp_addr}")

            while self.running:
                msg = self._tcp_recv(conn)
                if msg is None:
                    break
                if msg.get("type") == CTRL_UNSUBSCRIBE:
                    self._log("INFO", f"UNSUBSCRIBE '{name}'")
                    break
        except Exception as exc:
            pass
        finally:
            if ci is not None:
                with self.clients_lock:
                    self.clients.pop(ci.udp_addr, None)
                self._log("INFO", f"Client '{ci.name}' disconnected")
                self._push_clients()
            try: conn.close()
            except OSError: pass

    def _udp_recv_loop(self):
        while self.running:
            try:
                data, src = self.udp_sock.recvfrom(MAX_UDP_BUFFER)
            except socket.timeout:
                continue
            except OSError:
                break
            matched_addr, ci, msg_type, seq_num = self._match_udp_sender(src, data)
            if ci is None:
                continue
            if matched_addr != src:
                self._remap_client_udp_addr(matched_addr, src, ci)
            if msg_type == MSG_ACK:
                self._handle_ack(src, seq_num, ci)
            elif msg_type == MSG_HEARTBEAT_ACK:
                ci.last_heartbeat = time.time()

    def _match_udp_sender(self, src, data):
        with self.clients_lock:
            direct = self.clients.get(src)
        if direct is not None:
            try:
                msg_type, seq_num, _ = parse_udp_packet(data, direct.session_key)
                return src, direct, msg_type, seq_num
            except Exception:
                pass

        with self.clients_lock:
            snap = list(self.clients.items())
        same_ip = [(addr, ci) for addr, ci in snap if addr[0] == src[0]]
        rest = [(addr, ci) for addr, ci in snap if addr[0] != src[0]]

        for reg_addr, ci in same_ip + rest:
            try:
                msg_type, seq_num, _ = parse_udp_packet(data, ci.session_key)
                return reg_addr, ci, msg_type, seq_num
            except Exception:
                continue
        return None, None, None, None

    def _remap_client_udp_addr(self, old_addr, new_addr, ci):
        if old_addr == new_addr:
            return

        with self.clients_lock:
            current = self.clients.get(old_addr)
            if current is None:
                return
            self.clients.pop(old_addr, None)
            current.udp_addr = new_addr
            self.clients[new_addr] = current

        with self.pending_lock:
            for pm in self.pending_acks.values():
                if old_addr in pm.encrypted_data:
                    pm.encrypted_data[new_addr] = pm.encrypted_data.pop(old_addr)
                if old_addr in pm.retries:
                    pm.retries[new_addr] = pm.retries.pop(old_addr)
                if old_addr in pm.last_sent:
                    pm.last_sent[new_addr] = pm.last_sent.pop(old_addr)
                if old_addr in pm.pending_clients:
                    pm.pending_clients.discard(old_addr)
                    pm.pending_clients.add(new_addr)

        self._log(
            "INFO",
            f"UDP endpoint updated for '{ci.name}': {old_addr[0]}:{old_addr[1]} → {new_addr[0]}:{new_addr[1]}",
        )
        self._push_clients()

    def _handle_ack(self, udp_addr, seq_num, ci):
        self.stat_acks_received += 1
        ci.notifications_acked += 1
        with self.pending_lock:
            pm = self.pending_acks.get(seq_num)
            if pm is None:
                return
            pm.pending_clients.discard(udp_addr)
            if not pm.pending_clients:
                del self.pending_acks[seq_num]
                self._log("INFO", f"Notification #{seq_num} fully acknowledged")

    def _retransmit_loop(self):
        while self.running:
            time.sleep(CHECK_INTERVAL)
            now = time.time()
            evict, seqs_done = [], []
            with self.pending_lock:
                for seq_num, pm in list(self.pending_acks.items()):
                    for udp_addr in list(pm.pending_clients):
                        elapsed = now - pm.last_sent.get(udp_addr, pm.created_at)
                        if elapsed < RETRANSMIT_TIMEOUT:
                            continue
                        retries = pm.retries.get(udp_addr, 0)
                        if retries >= MAX_RETRIES:
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
                                except OSError:
                                    pm.pending_clients.discard(udp_addr)
                                    evict.append(udp_addr)
                    if not pm.pending_clients:
                        seqs_done.append(seq_num)
                for s in seqs_done:
                    self.pending_acks.pop(s, None)
            if evict:
                with self.clients_lock:
                    for ua in evict:
                        gone = self.clients.pop(ua, None)
                        if gone:
                            self._log("WARN", f"Evicted unresponsive client '{gone.name}'")
                self._push_clients()

    def _heartbeat_loop(self):
        while self.running:
            time.sleep(HEARTBEAT_INTERVAL)
            now  = time.time()
            dead = []
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
                    for ua in dead:
                        gone = self.clients.pop(ua, None)
                        if gone:
                            self._log("WARN", f"Heartbeat timeout – evicted '{gone.name}'")
                self._push_clients()

    def _next_seq(self):
        with self._seq_lock:
            self._seq += 1
            return self._seq

    @staticmethod
    def _tcp_send(conn, obj):
        data = json.dumps(obj).encode("utf-8")
        conn.sendall(len(data).to_bytes(4, "big") + data)

    @staticmethod
    def _tcp_recv(conn):
        try:
            raw = b""
            while len(raw) < 4:
                chunk = conn.recv(4 - len(raw))
                if not chunk: return None
                raw += chunk
            length = int.from_bytes(raw, "big")
            if length > 65535: return None
            buf = b""
            while len(buf) < length:
                chunk = conn.recv(length - len(buf))
                if not chunk: return None
                buf += chunk
            return json.loads(buf.decode("utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return None

    def get_stats(self):
        with self.pending_lock:
            pending = sum(len(p.pending_clients) for p in self.pending_acks.values())
        with self.clients_lock:
            n = len(self.clients)
        return {
            "clients":      n,
            "broadcasts":   self.stat_broadcast_count,
            "acks":         self.stat_acks_received,
            "retransmits":  self.stat_retransmit_count,
            "pending_acks": pending,
        }


# ── Embedded client ────────────────────────────────────────────────────────────

class EmbeddedClient:
    """Notification client that puts received messages into a queue."""

    def __init__(self, client_name, notif_q: queue.Queue, log_q: queue.Queue,
                 server_host="localhost", server_tcp_port=9000,
                 udp_listen_port=0, cafile="server.crt"):
        self.client_name      = client_name
        self._notif_q         = notif_q
        self._log_q           = log_q
        self.server_host      = server_host
        self.server_tcp_port  = server_tcp_port
        self.udp_listen_port  = udp_listen_port
        self.cafile           = cafile

        self.session_key:    Optional[bytes] = None
        self.server_udp_port: Optional[int]  = None
        self.tcp_conn:  Optional[ssl.SSLSocket]  = None
        self.udp_sock:  Optional[socket.socket]  = None
        self._seen_seqs      = set()
        self._seen_lock      = threading.Lock()
        self.stat_received   = 0
        self.stat_duplicates = 0
        self.stat_acks_sent  = 0
        self.running         = False

        self.ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.ssl_ctx.load_verify_locations(cafile)
        self.ssl_ctx.check_hostname = False
        self.ssl_ctx.verify_mode    = ssl.CERT_REQUIRED

    def _log(self, level, msg):
        ts = time.strftime("%H:%M:%S")
        self._log_q.put((level, f"[{ts}]  {msg}"))

    def connect(self):
        self.udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp_sock.bind(("", self.udp_listen_port))
        self.udp_sock.settimeout(1.0)
        actual_udp = self.udp_sock.getsockname()[1]
        self._log("INFO", f"UDP listener on port {actual_udp}")

        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw.connect((self.server_host, self.server_tcp_port))
        self.tcp_conn = self.ssl_ctx.wrap_socket(raw, server_hostname=self.server_host)
        self._log("INFO", f"TLS connected to {self.server_host}:{self.server_tcp_port}  "
                          f"{self.tcp_conn.version()}  cipher={self.tcp_conn.cipher()[0]}")

        self._tcp_send({
            "type":        CTRL_SUBSCRIBE,
            "client_name": self.client_name,
            "udp_port":    actual_udp,
        })
        resp = self._tcp_recv()
        if resp is None or resp.get("type") != CTRL_SUB_ACK:
            raise ConnectionError(f"Subscription failed: {resp}")

        self.session_key     = base64.b64decode(resp["session_key"])
        self.server_udp_port = int(resp["server_udp_port"])
        self._log("INFO", f"Subscribed – session key {len(self.session_key)} bytes, "
                          f"server UDP port {self.server_udp_port}")
        self._send_udp_bootstrap()
        return self

    def _send_udp_bootstrap(self):
        """Send one UDP packet immediately so server can learn the real endpoint."""
        if not self.session_key or self.server_udp_port is None:
            return
        try:
            server_ip = self.tcp_conn.getpeername()[0]
            pkt = build_udp_packet(MSG_HEARTBEAT_ACK, 0, self.session_key, b"")
            self.udp_sock.sendto(pkt, (server_ip, self.server_udp_port))
            self._log("INFO", f"UDP bootstrap sent to {server_ip}:{self.server_udp_port}")
        except OSError as exc:
            self._log("WARN", f"UDP bootstrap failed: {exc}")

    def start_listening(self):
        self.running = True
        t = threading.Thread(target=self._udp_recv_loop, daemon=True)
        t.start()
        self._log("INFO", f"Client '{self.client_name}' ready – listening for notifications")

    def disconnect(self):
        self._log("INFO", "Disconnecting …")
        self.running = False
        if self.tcp_conn:
            try: self._tcp_send({"type": CTRL_UNSUBSCRIBE})
            except OSError: pass
            try: self.tcp_conn.close()
            except OSError: pass
        if self.udp_sock:
            try: self.udp_sock.close()
            except OSError: pass
        self._log("INFO", f"Final stats – received={self.stat_received}, "
                          f"duplicates={self.stat_duplicates}, "
                          f"acks_sent={self.stat_acks_sent}")

    def _udp_recv_loop(self):
        while self.running:
            try:
                data, addr = self.udp_sock.recvfrom(MAX_UDP_BUFFER)
            except socket.timeout:
                continue
            except OSError:
                break
            self._dispatch_udp(data, addr)

    def _dispatch_udp(self, data, addr):
        if not self.session_key: return
        try:
            msg_type, seq_num, payload = parse_udp_packet(data, self.session_key)
        except Exception as exc:
            self._log("WARN", f"Decrypt/auth failed from {addr}: {exc}")
            return
        if msg_type == MSG_NOTIFICATION:
            self._handle_notification(seq_num, payload, addr)
        elif msg_type == MSG_HEARTBEAT:
            self._send_heartbeat_ack(seq_num, addr)

    def _handle_notification(self, seq_num, payload, addr):
        self._send_ack(seq_num, addr)
        with self._seen_lock:
            dup = seq_num in self._seen_seqs
            if not dup:
                self._seen_seqs.add(seq_num)
                if len(self._seen_seqs) > 1000:
                    cutoff = max(self._seen_seqs) - 500
                    self._seen_seqs = {s for s in self._seen_seqs if s > cutoff}
        if dup:
            self.stat_duplicates += 1
            return
        self.stat_received += 1
        message = payload.decode("utf-8", errors="replace")
        ts = time.strftime("%H:%M:%S")
        self._notif_q.put((seq_num, ts, message))
        self._log("INFO", f"Notification #{seq_num}: {message[:80]}")

    def _send_ack(self, seq_num, server_addr):
        dst = (server_addr[0], self.server_udp_port)
        pkt = build_udp_packet(MSG_ACK, seq_num, self.session_key, b"")
        try:
            self.udp_sock.sendto(pkt, dst)
            self.stat_acks_sent += 1
        except OSError: pass

    def _send_heartbeat_ack(self, seq_num, server_addr):
        dst = (server_addr[0], self.server_udp_port)
        pkt = build_udp_packet(MSG_HEARTBEAT_ACK, seq_num, self.session_key, b"")
        try: self.udp_sock.sendto(pkt, dst)
        except OSError: pass

    def _tcp_send(self, obj):
        data = json.dumps(obj).encode("utf-8")
        self.tcp_conn.sendall(len(data).to_bytes(4, "big") + data)

    def _tcp_recv(self):
        try:
            raw = b""
            while len(raw) < 4:
                chunk = self.tcp_conn.recv(4 - len(raw))
                if not chunk: return None
                raw += chunk
            length = int.from_bytes(raw, "big")
            if length > 65535: return None
            buf = b""
            while len(buf) < length:
                chunk = self.tcp_conn.recv(length - len(buf))
                if not chunk: return None
                buf += chunk
            return json.loads(buf.decode("utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return None


# ─────────────────────────────────────────────────────────────────────────────
# GUI
# ─────────────────────────────────────────────────────────────────────────────

def style_widget(w, **kw):
    try: w.configure(**kw)
    except tk.TclError: pass


class JackfruitGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("🍈  Jackfruit Notification System")
        root.geometry("900x680")
        root.minsize(780, 560)
        root.configure(bg=BG)

        self._server: Optional[EmbeddedServer] = None
        self._client: Optional[EmbeddedClient] = None

        self._srv_log_q    = queue.Queue()
        self._srv_clients_q = queue.Queue()
        self._cli_notif_q  = queue.Queue()
        self._cli_log_q    = queue.Queue()

        self._build_ui()
        self._poll()

    # ── UI construction ───────────────────────────────────────────────────

    def _build_ui(self):
        # Title bar
        title_frame = tk.Frame(self.root, bg=BG, pady=10)
        title_frame.pack(fill="x", padx=20)
        tk.Label(title_frame, text="🍈  Jackfruit Notification System",
                 bg=BG, fg=ACCENT,
                 font=("Segoe UI", 16, "bold")).pack(side="left")

        # Notebook
        style = ttk.Style()
        style.theme_use("default")
        style.configure("TNotebook",        background=BG,  borderwidth=0)
        style.configure("TNotebook.Tab",    background=BG2, foreground=SUBTEXT,
                        padding=[16, 8],    font=("Segoe UI", 10))
        style.map("TNotebook.Tab",
                  background=[("selected", BG3)],
                  foreground=[("selected", ACCENT)])

        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=16, pady=(0, 16))

        srv_frame = tk.Frame(nb, bg=BG)
        cli_frame = tk.Frame(nb, bg=BG)
        nb.add(srv_frame, text="  🖥  Server  ")
        nb.add(cli_frame, text="  📡  Client  ")

        self._build_server_tab(srv_frame)
        self._build_client_tab(cli_frame)

    # ── helpers ───────────────────────────────────────────────────────────

    def _label(self, parent, text, fg=SUBTEXT, size=9, bold=False):
        f = ("Segoe UI", size, "bold") if bold else ("Segoe UI", size)
        return tk.Label(parent, text=text, bg=BG, fg=fg, font=f)

    def _entry(self, parent, textvariable=None, width=20, placeholder=""):
        e = tk.Entry(parent, textvariable=textvariable, width=width,
                     bg=BG2, fg=TEXT, insertbackground=TEXT,
                     relief="flat", bd=4,
                     font=("Consolas", 10))
        return e

    def _button(self, parent, text, command, color=ACCENT, width=14):
        return tk.Button(parent, text=text, command=command,
                         bg=color, fg=BG, activebackground=TEXT,
                         font=("Segoe UI", 9, "bold"),
                         relief="flat", bd=0, padx=8, pady=5,
                         cursor="hand2", width=width)

    def _logbox(self, parent, height=10):
        box = scrolledtext.ScrolledText(
            parent, height=height, bg=BG2, fg=TEXT,
            font=("Consolas", 9), relief="flat", bd=0,
            insertbackground=TEXT, state="disabled",
            wrap="word"
        )
        box.tag_configure("INFO",  foreground=TEXT)
        box.tag_configure("WARN",  foreground=YELLOW)
        box.tag_configure("ERROR", foreground=RED)
        box.tag_configure("OK",    foreground=GREEN)
        box.tag_configure("NOTIF", foreground=ACCENT)
        return box

    def _append_log(self, box: scrolledtext.ScrolledText, level: str, msg: str):
        box.configure(state="normal")
        box.insert("end", msg + "\n", level)
        box.see("end")
        box.configure(state="disabled")

    def _section(self, parent, title):
        f = tk.LabelFrame(parent, text=f"  {title}  ",
                          bg=BG, fg=ACCENT,
                          font=("Segoe UI", 9, "bold"),
                          relief="flat", bd=1,
                          highlightbackground=BORDER,
                          highlightthickness=1)
        return f

    # ── Server tab ─────────────────────────────────────────────────────────

    def _build_server_tab(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(2, weight=1)

        # Config row
        cfg = self._section(parent, "Server Configuration")
        cfg.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 6))

        self._srv_host = tk.StringVar(value="0.0.0.0")
        self._srv_tcp  = tk.StringVar(value="9000")
        self._srv_udp  = tk.StringVar(value="9001")
        self._srv_cert = tk.StringVar(value="server.crt")
        self._srv_key  = tk.StringVar(value="server.key")

        fields = [
            ("Bind host",   self._srv_host, 14),
            ("TCP port",    self._srv_tcp,  7),
            ("UDP port",    self._srv_udp,  7),
            ("Cert file",   self._srv_cert, 16),
            ("Key file",    self._srv_key,  16),
        ]
        for i, (lbl, var, w) in enumerate(fields):
            self._label(cfg, lbl).grid(row=0, column=i*2, padx=(10,2), pady=8, sticky="e")
            self._entry(cfg, textvariable=var, width=w).grid(
                row=0, column=i*2+1, padx=(0, 10), pady=8)

        # Start / Stop buttons
        btns = tk.Frame(parent, bg=BG)
        btns.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 6))

        self._srv_status_var = tk.StringVar(value="⏹  Stopped")
        tk.Label(btns, textvariable=self._srv_status_var, bg=BG,
                 fg=SUBTEXT, font=("Segoe UI", 10)).pack(side="left", padx=4)

        self._srv_stop_btn  = self._button(btns, "⏹  Stop Server",
                                            self._stop_server, color=RED, width=16)
        self._srv_start_btn = self._button(btns, "▶  Start Server",
                                            self._start_server, color=GREEN, width=16)
        self._srv_stop_btn.pack(side="right", padx=4)
        self._srv_start_btn.pack(side="right", padx=4)
        self._srv_stop_btn.configure(state="disabled")

        # Lower area: log + clients side by side
        lower = tk.Frame(parent, bg=BG)
        lower.grid(row=2, column=0, sticky="nsew", padx=12, pady=(0, 4))
        lower.columnconfigure(0, weight=3)
        lower.columnconfigure(1, weight=1)
        lower.rowconfigure(0, weight=1)

        # Log
        log_sec = self._section(lower, "Server Log")
        log_sec.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        log_sec.rowconfigure(0, weight=1)
        log_sec.columnconfigure(0, weight=1)
        self._srv_log = self._logbox(log_sec, height=14)
        self._srv_log.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)

        # Clients
        cli_sec = self._section(lower, "Connected Clients")
        cli_sec.grid(row=0, column=1, sticky="nsew")
        cli_sec.rowconfigure(1, weight=1)
        cli_sec.columnconfigure(0, weight=1)

        self._clients_tree = ttk.Treeview(cli_sec,
            columns=("name", "addr", "sent", "acked"),
            show="headings", height=12)
        for col, hdr, w in [("name","Name",90),("addr","UDP Addr",110),
                             ("sent","Sent",40),("acked","ACKd",40)]:
            self._clients_tree.heading(col, text=hdr)
            self._clients_tree.column(col, width=w, anchor="center")

        style = ttk.Style()
        style.configure("Treeview",
                        background=BG2, fieldbackground=BG2,
                        foreground=TEXT, rowheight=22,
                        font=("Consolas", 9))
        style.configure("Treeview.Heading",
                        background=BG3, foreground=ACCENT,
                        font=("Segoe UI", 9, "bold"))
        self._clients_tree.grid(row=1, column=0, sticky="nsew", padx=6, pady=(0,6))

        # Stats row
        stats_row = tk.Frame(lower, bg=BG)
        stats_row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        self._stats_labels = {}
        for key, label in [("broadcasts","Broadcasts"), ("acks","ACKs Received"),
                            ("retransmits","Retransmits"), ("pending_acks","Pending ACKs")]:
            frm = tk.Frame(stats_row, bg=BG3, padx=10, pady=4)
            frm.pack(side="left", padx=4)
            tk.Label(frm, text=label, bg=BG3, fg=SUBTEXT,
                     font=("Segoe UI", 8)).pack()
            v = tk.StringVar(value="0")
            tk.Label(frm, textvariable=v, bg=BG3, fg=ACCENT,
                     font=("Consolas", 14, "bold")).pack()
            self._stats_labels[key] = v

        # Broadcast bar
        bcast = tk.Frame(parent, bg=BG)
        bcast.grid(row=3, column=0, sticky="ew", padx=12, pady=(4, 10))
        self._label(bcast, "Message:", size=10).pack(side="left", padx=(0, 6))
        self._bcast_var = tk.StringVar()
        bcast_entry = self._entry(bcast, textvariable=self._bcast_var, width=52)
        bcast_entry.pack(side="left", padx=(0, 8), ipady=4, fill="x", expand=True)
        bcast_entry.bind("<Return>", lambda e: self._broadcast())
        self._button(bcast, "📢  Broadcast", self._broadcast,
                     color=ACCENT, width=14).pack(side="left")

    # ── Client tab ─────────────────────────────────────────────────────────

    def _build_client_tab(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(2, weight=1)

        # Config
        cfg = self._section(parent, "Connection Settings")
        cfg.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 6))

        self._cli_name   = tk.StringVar(value="Alice")
        self._cli_host   = tk.StringVar(value="localhost")
        self._cli_tcp    = tk.StringVar(value="9000")
        self._cli_cafile = tk.StringVar(value="server.crt")

        fields = [
            ("Client name", self._cli_name,   14),
            ("Server host", self._cli_host,   14),
            ("TCP port",    self._cli_tcp,     7),
            ("CA file",     self._cli_cafile,  16),
        ]
        for i, (lbl, var, w) in enumerate(fields):
            self._label(cfg, lbl).grid(row=0, column=i*2, padx=(10,2), pady=8, sticky="e")
            self._entry(cfg, textvariable=var, width=w).grid(
                row=0, column=i*2+1, padx=(0, 10), pady=8)

        # Buttons row
        btns = tk.Frame(parent, bg=BG)
        btns.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 6))

        self._cli_status_var = tk.StringVar(value="⏹  Not connected")
        tk.Label(btns, textvariable=self._cli_status_var, bg=BG,
                 fg=SUBTEXT, font=("Segoe UI", 10)).pack(side="left", padx=4)

        self._cli_disconnect_btn = self._button(btns, "✖  Disconnect",
                                                 self._disconnect_client, RED, 14)
        self._cli_connect_btn    = self._button(btns, "🔗  Connect",
                                                 self._connect_client, GREEN, 14)
        self._cli_disconnect_btn.pack(side="right", padx=4)
        self._cli_connect_btn.pack(side="right", padx=4)
        self._cli_disconnect_btn.configure(state="disabled")

        # Notifications box
        notif_sec = self._section(parent, "Received Notifications")
        notif_sec.grid(row=2, column=0, sticky="nsew", padx=12, pady=(0, 4))
        notif_sec.rowconfigure(0, weight=1)
        notif_sec.columnconfigure(0, weight=1)

        self._notif_box = self._logbox(notif_sec, height=10)
        self._notif_box.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)
        self._notif_box.tag_configure("ts",    foreground=SUBTEXT,  font=("Consolas", 9))
        self._notif_box.tag_configure("seq",   foreground=YELLOW,   font=("Consolas", 9, "bold"))
        self._notif_box.tag_configure("text",  foreground=TEXT,     font=("Consolas", 10))
        self._notif_box.tag_configure("div",   foreground=BORDER,   font=("Consolas", 8))

        # Client log
        clog_sec = self._section(parent, "Client Log")
        clog_sec.grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 10))
        self._cli_log = self._logbox(clog_sec, height=5)
        self._cli_log.pack(fill="both", expand=True, padx=6, pady=6)

    # ── Server actions ────────────────────────────────────────────────────

    def _start_server(self):
        if self._server is not None:
            return
        cert = self._srv_cert.get().strip()
        key  = self._srv_key.get().strip()
        if not os.path.exists(cert) or not os.path.exists(key):
            messagebox.showerror("Certificate Error",
                f"Cannot find cert/key files:\n  {cert}\n  {key}\n\n"
                "Run  python generate_certs.py  first.")
            return
        try:
            tcp_port = int(self._srv_tcp.get())
            udp_port = int(self._srv_udp.get())
        except ValueError:
            messagebox.showerror("Config Error", "TCP/UDP ports must be integers.")
            return

        try:
            self._server = EmbeddedServer(
                log_q=self._srv_log_q,
                clients_q=self._srv_clients_q,
                tcp_host=self._srv_host.get().strip(),
                tcp_port=tcp_port,
                udp_port=udp_port,
                certfile=cert,
                keyfile=key,
            )
            self._server.start()
        except Exception as exc:
            messagebox.showerror("Server Error", str(exc))
            self._server = None
            return

        self._srv_start_btn.configure(state="disabled")
        self._srv_stop_btn.configure(state="normal")
        self._srv_status_var.set("▶  Running")

    def _stop_server(self):
        if self._server:
            self._server.stop()
            self._server = None
        self._srv_start_btn.configure(state="normal")
        self._srv_stop_btn.configure(state="disabled")
        self._srv_status_var.set("⏹  Stopped")

    def _broadcast(self):
        msg = self._bcast_var.get().strip()
        if not msg:
            return
        if self._server is None:
            messagebox.showwarning("Not Running", "Start the server first.")
            return
        self._server.broadcast(msg)
        self._bcast_var.set("")

    # ── Client actions ────────────────────────────────────────────────────

    def _connect_client(self):
        if self._client is not None:
            return
        try:
            tcp_port = int(self._cli_tcp.get())
        except ValueError:
            messagebox.showerror("Config Error", "TCP port must be an integer.")
            return

        def _do_connect():
            try:
                c = EmbeddedClient(
                    client_name=self._cli_name.get().strip() or "GUI-Client",
                    notif_q=self._cli_notif_q,
                    log_q=self._cli_log_q,
                    server_host=self._cli_host.get().strip(),
                    server_tcp_port=tcp_port,
                    cafile=self._cli_cafile.get().strip(),
                )
                c.connect()
                c.start_listening()
                self._client = c
                self.root.after(0, self._on_client_connected)
            except Exception as exc:
                self.root.after(0, lambda: (
                    messagebox.showerror("Connection Error", str(exc)),
                    self._on_client_disconnected()
                ))

        self._cli_connect_btn.configure(state="disabled")
        self._cli_status_var.set("🔄  Connecting …")
        threading.Thread(target=_do_connect, daemon=True).start()

    def _on_client_connected(self):
        self._cli_connect_btn.configure(state="disabled")
        self._cli_disconnect_btn.configure(state="normal")
        self._cli_status_var.set(f"🟢  Connected as '{self._cli_name.get()}'")

    def _on_client_disconnected(self):
        self._cli_connect_btn.configure(state="normal")
        self._cli_disconnect_btn.configure(state="disabled")
        self._cli_status_var.set("⏹  Not connected")

    def _disconnect_client(self):
        if self._client:
            threading.Thread(target=self._client.disconnect, daemon=True).start()
            self._client = None
        self._on_client_disconnected()

    # ── Polling loop ──────────────────────────────────────────────────────

    def _poll(self):
        # Server log
        while not self._srv_log_q.empty():
            level, msg = self._srv_log_q.get_nowait()
            self._append_log(self._srv_log, level, msg)

        # Clients table
        while not self._srv_clients_q.empty():
            snap = self._srv_clients_q.get_nowait()
            self._clients_tree.delete(*self._clients_tree.get_children())
            for name, addr, sent, acked in snap:
                self._clients_tree.insert("", "end", values=(name, addr, sent, acked))

        # Server stats
        if self._server:
            s = self._server.get_stats()
            for k, v in self._stats_labels.items():
                v.set(str(s.get(k, 0)))

        # Client notifications
        while not self._cli_notif_q.empty():
            seq, ts, msg = self._cli_notif_q.get_nowait()
            box = self._notif_box
            box.configure(state="normal")
            box.insert("end", "─" * 62 + "\n", "div")
            box.insert("end", f"[{ts}]  ", "ts")
            box.insert("end", f"#{seq}  ", "seq")
            box.insert("end", msg + "\n", "text")
            box.see("end")
            box.configure(state="disabled")

        # Client log
        while not self._cli_log_q.empty():
            level, msg = self._cli_log_q.get_nowait()
            self._append_log(self._cli_log, level, msg)

        self.root.after(250, self._poll)

    def on_close(self):
        if self._server:
            self._server.stop()
        if self._client:
            self._client.disconnect()
        self.root.destroy()


# ─────────────────────────────────────────────────────────────────────────────

def main():
    root = tk.Tk()
    app  = JackfruitGUI(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
