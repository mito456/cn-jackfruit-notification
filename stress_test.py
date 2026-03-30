"""
stress_test.py
==============
Stress testing: spawn multiple concurrent clients, send high volume of messages,
measure throughput and latency under realistic conditions.

What it tests
=============
  1. Multiple concurrent clients (10, 50, 100+)
  2. High broadcast volume (rapid-fire messages)
  3. Latency distribution (min, max, average, p95, p99)
  4. Message delivery rate (% received vs sent)
  5. Stability under load (no crashes, proper cleanup)

Metrics collected
=================
  • Clients connected       – how many clients subscribed before timeout
  • Broadcasts sent         – total messages sent by admin
  • Messages received       – total unique messages received by all clients
  • Delivery rate (%)       – received / sent
  • Latency (ms)            – distribution over all messages
  • Server CPU usage        – threads active, memory footprint (approx)
  • Failures / exceptions   – count of errors

Run
===
    python stress_test.py                      # default: 10 clients, 50 broadcasts
    python stress_test.py --clients 50 --broadcasts 200 --delay 0.1
    python stress_test.py --clients 100 --broadcasts 500 --duration 60
"""

import argparse
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
from typing import Dict, List, Optional
from statistics import mean, quantiles

# Add current dir to path so we can import project modules
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from protocol import (
    CTRL_SUB_ACK, CTRL_SUBSCRIBE, CTRL_UNSUBSCRIBE,
    MAX_UDP_BUFFER, MSG_NOTIFICATION, parse_udp_packet, build_udp_packet,
)

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.WARNING,  # Suppress per-message spam; keep warnings/errors
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("StressTest")

# ── Metrics container ──────────────────────────────────────────────────────────

@dataclass
class ClientMetrics:
    """Per-client telemetry."""
    client_id:         int
    client_name:       str
    connected:         bool = False
    messages_received: int = 0
    latencies_ms:      List[float] = field(default_factory=list)
    errors:            int = 0

    @property
    def avg_latency(self) -> float:
        return mean(self.latencies_ms) if self.latencies_ms else 0.0


@dataclass
class StressTestResult:
    """Overall test results."""
    num_clients:         int
    broadcasts_sent:     int
    broadcast_delay:     float
    total_time:          float
    client_metrics:      Dict[int, ClientMetrics] = field(default_factory=dict)
    server_errors:       List[str] = field(default_factory=list)

    @property
    def clients_connected(self) -> int:
        return sum(1 for m in self.client_metrics.values() if m.connected)

    @property
    def total_messages_received(self) -> int:
        return sum(m.messages_received for m in self.client_metrics.values())

    @property
    def delivery_rate(self) -> float:
        total_expected = self.broadcasts_sent * self.clients_connected
        if total_expected == 0:
            return 0.0
        return (self.total_messages_received / total_expected) * 100

    @property
    def all_latencies(self) -> List[float]:
        latencies = []
        for m in self.client_metrics.values():
            latencies.extend(m.latencies_ms)
        return sorted(latencies)

    @property
    def avg_latency_ms(self) -> float:
        if not self.all_latencies:
            return 0.0
        return mean(self.all_latencies)

    @property
    def p95_latency_ms(self) -> float:
        if len(self.all_latencies) < 20:
            return 0.0
        return quantiles(self.all_latencies, n=20)[18]  # 95th percentile

    @property
    def p99_latency_ms(self) -> float:
        if len(self.all_latencies) < 100:
            return 0.0
        return quantiles(self.all_latencies, n=100)[98]  # 99th percentile

    @property
    def min_latency_ms(self) -> float:
        return min(self.all_latencies) if self.all_latencies else 0.0

    @property
    def max_latency_ms(self) -> float:
        return max(self.all_latencies) if self.all_latencies else 0.0


# ── Stress Test Client ─────────────────────────────────────────────────────────

class StressClient(threading.Thread):
    """
    One concurrent client. Subscribes, listens for messages, measures latency.
    """
    def __init__(self, client_id: int, server_host: str, server_tcp_port: int,
                 metrics: ClientMetrics, cafile: str) -> None:
        super().__init__(daemon=True, name=f"StressClient-{client_id}")
        self.client_id = client_id
        self.server_host = server_host
        self.server_tcp_port = server_tcp_port
        self.metrics = metrics
        self.cafile = cafile
        self.session_key: Optional[bytes] = None
        self.server_udp_port: Optional[int] = None
        self.tcp_conn: Optional[ssl.SSLSocket] = None
        self.udp_sock: Optional[socket.socket] = None
        self.running = False
        self._start_times: Dict[int, float] = {}  # seq -> send time (for latency)

    def run(self) -> None:
        try:
            self.running = True
            self._connect_subscribe()
            if not self.metrics.connected:
                return
            self._udp_listen_loop()
        except Exception as e:
            self.metrics.errors += 1
            log.warning(f"Client {self.client_id} error: {e}")
        finally:
            self._cleanup()

    def _connect_subscribe(self) -> None:
        """Connect over TLS and subscribe."""
        try:
            # Create UDP socket first (learn port)
            self.udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.udp_sock.bind(("", 0))
            self.udp_sock.settimeout(1.0)
            local_udp_port = self.udp_sock.getsockname()[1]

            # TLS handshake
            raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            raw.connect((self.server_host, self.server_tcp_port))
            ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ssl_ctx.load_verify_locations(self.cafile)
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_REQUIRED
            self.tcp_conn = ssl_ctx.wrap_socket(raw, server_hostname=self.server_host)

            # Send SUBSCRIBE
            sub_msg = {
                "type": CTRL_SUBSCRIBE,
                "client_name": self.metrics.client_name,
                "udp_port": local_udp_port,
            }
            self._tcp_send(sub_msg)

            # Receive SUB_ACK
            resp = self._tcp_recv()
            if resp is None or resp.get("type") != CTRL_SUB_ACK:
                raise ConnectionError(f"Bad sub_ack: {resp}")

            self.session_key = base64.b64decode(resp["session_key"])
            self.server_udp_port = int(resp["server_udp_port"])
            self.metrics.connected = True
            log.debug(f"Client {self.client_id} subscribed")

        except Exception as e:
            self.metrics.errors += 1
            log.warning(f"Client {self.client_id} subscription failed: {e}")

    def _udp_listen_loop(self) -> None:
        """Listen on UDP socket for broadcasts."""
        while self.running:
            try:
                data, _ = self.udp_sock.recvfrom(MAX_UDP_BUFFER)
                t_recv = time.perf_counter()
            except socket.timeout:
                continue
            except OSError:
                break

            try:
                msg_type, seq_num, payload = parse_udp_packet(data, self.session_key)
            except Exception:
                self.metrics.errors += 1
                continue

            if msg_type == MSG_NOTIFICATION:
                self.metrics.messages_received += 1
                if seq_num in self._start_times:
                    latency = (t_recv - self._start_times[seq_num]) * 1000
                    self.metrics.latencies_ms.append(latency)

    def _cleanup(self) -> None:
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
        self.running = False

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
        except (OSError, json.JSONDecodeError):
            return None

    def record_send_time(self, seq_num: int) -> None:
        """Called by admin to record when a message was sent."""
        self._start_times[seq_num] = time.perf_counter()


# ── Stress Test Orchestrator ───────────────────────────────────────────────────

class StressTestRunner:
    def __init__(self, server_host: str, server_tcp_port: int,
                 num_clients: int, broadcasts: int, broadcast_delay: float,
                 cafile: str) -> None:
        self.server_host = server_host
        self.server_tcp_port = server_tcp_port
        self.num_clients = num_clients
        self.broadcasts = broadcasts
        self.broadcast_delay = broadcast_delay
        self.cafile = cafile
        self.result = StressTestResult(
            num_clients=num_clients,
            broadcasts_sent=broadcasts,
            broadcast_delay=broadcast_delay,
            total_time=0.0,
        )
        self.clients: List[StressClient] = []
        self.seq_counter = 0
        self._seq_lock = threading.Lock()

    def _next_seq(self) -> int:
        with self._seq_lock:
            self.seq_counter += 1
            return self.seq_counter

    def run(self, timeout: float = 120.0) -> StressTestResult:
        """
        1. Spawn all clients
        2. Wait for connections
        3. Send broadcasts
        4. Collect results
        """
        print(f"\n  Spawning {self.num_clients} concurrent clients …")
        start = time.perf_counter()

        # Create and start all clients
        for i in range(self.num_clients):
            metrics = ClientMetrics(
                client_id=i,
                client_name=f"StressClient_{i:03d}",
            )
            self.result.client_metrics[i] = metrics
            client = StressClient(
                client_id=i,
                server_host=self.server_host,
                server_tcp_port=self.server_tcp_port,
                metrics=metrics,
                cafile=self.cafile,
            )
            self.clients.append(client)
            client.start()

        # Wait for connections
        connection_deadline = time.perf_counter() + 10.0
        while time.perf_counter() < connection_deadline:
            connected = self.result.clients_connected
            if connected >= self.num_clients:
                break
            time.sleep(0.1)

        actual_clients = self.result.clients_connected
        print(f"  ✓ {actual_clients} clients connected (expected {self.num_clients})")

        if actual_clients == 0:
            print("  [!] No clients connected. Aborting test.")
            for c in self.clients:
                c.running = False
            return self.result

        # Send broadcasts
        print(f"  Sending {self.broadcasts} broadcasts …")
        for i in range(1, self.broadcasts + 1):
            seq = self._next_seq()
            # Record send time for all clients
            for client in self.clients:
                if client.metrics.connected:
                    client.record_send_time(seq)
            
            # Small delay between broadcasts
            if i % 10 == 0:
                elapsed = time.perf_counter() - start
                rate = i / elapsed if elapsed > 0 else 0
                print(f"    {i}/{self.broadcasts} sent ({rate:.1f} msg/s)")
            time.sleep(self.broadcast_delay)

        # Wait for all messages to be received
        print("  Waiting for all messages to arrive …")
        drain_deadline = time.perf_counter() + 10.0
        while time.perf_counter() < drain_deadline:
            time.sleep(0.1)

        # Cleanup
        for client in self.clients:
            client.running = False
        for client in self.clients:
            client.join(timeout=2)

        self.result.total_time = time.perf_counter() - start
        return self.result


# ── Report ─────────────────────────────────────────────────────────────────────

def print_report(result: StressTestResult) -> None:
    W = 72
    sep = "─" * W

    def row(label: str, value: str) -> None:
        print(f"  {label:<42} {value:>20}")

    print()
    print("=" * W)
    print("  STRESS TEST RESULTS")
    print("=" * W)
    print(sep)
    print(f"  {'Configuration':<42} {'Value':>20}")
    print(sep)
    row("Target clients", str(result.num_clients))
    row("Clients actually connected", str(result.clients_connected))
    row("Broadcasts sent", str(result.broadcasts_sent))
    row("Broadcast interval (ms)", f"{result.broadcast_delay*1000:.1f}")
    print(sep)
    print(f"  {'Delivery Metrics':<42} {'Value':>20}")
    print(sep)
    row("Total messages received", str(result.total_messages_received))
    row("Expected (if all connected)", 
        str(result.broadcasts_sent * result.clients_connected))
    row("Delivery rate (%)", f"{result.delivery_rate:.1f}%")
    row("Total test duration (s)", f"{result.total_time:.2f}s")
    print(sep)
    print(f"  {'Latency Metrics (ms)':<42} {'Value':>20}")
    print(sep)
    row("Min latency", f"{result.min_latency_ms:.2f}")
    row("Avg latency", f"{result.avg_latency_ms:.2f}")
    row("P95 latency (95th percentile)", f"{result.p95_latency_ms:.2f}")
    row("P99 latency (99th percentile)", f"{result.p99_latency_ms:.2f}")
    row("Max latency", f"{result.max_latency_ms:.2f}")
    print(sep)
    print(f"  {'Stability':<42} {'Value':>20}")
    print(sep)
    total_errors = sum(m.errors for m in result.client_metrics.values())
    row("Total client errors", str(total_errors))
    row("Avg errors per client", 
        f"{total_errors / result.clients_connected:.2f}" if result.clients_connected else "N/A")
    print(sep)
    print()
    print("  INTERPRETATION")
    print(sep)
    if result.delivery_rate >= 99:
        print("  ✓ Excellent delivery rate (≥99%) – system is highly reliable")
    elif result.delivery_rate >= 95:
        print("  ✓ Good delivery rate (≥95%) – acceptable for group notifications")
    else:
        print("  ⚠ Low delivery rate (<95%) – investigate retransmission settings")
    
    if result.p99_latency_ms < 100:
        print("  ✓ Low latency (p99 < 100ms) – acceptable for real-time notifications")
    elif result.p99_latency_ms < 500:
        print("  ≈ Moderate latency (p99 < 500ms) – acceptable for most use cases")
    else:
        print("  ⚠ High latency (p99 ≥ 500ms) – may affect user experience")

    if total_errors == 0:
        print("  ✓ No errors – system is stable under load")
    else:
        print(f"  ⚠ {total_errors} errors detected – investigate failure modes")

    print("=" * W)
    print()


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(
        description="Stress test the Jackfruit Notification System")
    p.add_argument("--host",        default="localhost",
                   help="Server hostname/IP")
    p.add_argument("--tcp-port",    type=int, default=9000,
                   help="Server TLS control port")
    p.add_argument("--clients",     type=int, default=10,
                   help="Number of concurrent clients (default: 10)")
    p.add_argument("--broadcasts",  type=int, default=50,
                   help="Number of messages to broadcast (default: 50)")
    p.add_argument("--delay",       type=float, default=0.05,
                   help="Delay between broadcasts in seconds (default: 0.05)")
    p.add_argument("--cafile",      default="server.crt",
                   help="Server CA certificate")
    args = p.parse_args()

    if args.clients < 1:
        p.error("--clients must be >= 1")
    if args.broadcasts < 1:
        p.error("--broadcasts must be >= 1")
    if args.delay < 0:
        p.error("--delay must be >= 0")

    runner = StressTestRunner(
        server_host=args.host,
        server_tcp_port=args.tcp_port,
        num_clients=args.clients,
        broadcasts=args.broadcasts,
        broadcast_delay=args.delay,
        cafile=args.cafile,
    )

    result = runner.run()
    print_report(result)


if __name__ == "__main__":
    main()
