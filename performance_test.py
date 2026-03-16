"""
performance_test.py
-------------------
Standalone performance comparison: Reliable UDP vs Best-Effort UDP.

What it does
============
Spins up two in-process experiments using raw UDP sockets on localhost:

  1. Best-Effort UDP  – sender fires packets and forgets; no ACK, no retransmit.
  2. Reliable UDP     – sender waits for ACK per packet; retransmits on timeout
                        (mirrors the mechanism in server.py).

An artificial packet-loss filter (configurable %) is applied to the receiver
to simulate a lossy network without needing a real WAN.

Metrics collected
=================
  • Delivery rate  (%)      – packets received / packets sent
  • Average latency (ms)    – RTT for reliable; one-way for best-effort (approx)
  • Total time (s)          – wall-clock for entire experiment
  • Retransmissions         – reliable mode only
  • Bandwidth overhead (%)  – extra bytes due to ACKs and retransmissions

Run
===
    python performance_test.py
    python performance_test.py --packets 200 --loss 20 --timeout 0.3
"""

import argparse
import os
import random
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional

# ── Tiny wire format (independent of the main project) ───────────────────────
# Header: TYPE(1B) | SEQ(4B) = 5 bytes
_FMT = "!BI"
_HDR = struct.calcsize(_FMT)   # 5

MSG_DATA = 0x01
MSG_ACK  = 0x02


def _pack(msg_type: int, seq: int, payload: bytes = b"") -> bytes:
    return struct.pack(_FMT, msg_type, seq) + payload


def _unpack(data: bytes):
    if len(data) < _HDR:
        return None, None, b""
    msg_type, seq = struct.unpack(_FMT, data[:_HDR])
    return msg_type, seq, data[_HDR:]


# ── Result container ──────────────────────────────────────────────────────────

@dataclass
class ExperimentResult:
    mode:             str
    packets_sent:     int = 0
    packets_received: int = 0
    retransmissions:  int = 0
    total_bytes_sent: int = 0  # includes retransmissions
    ack_bytes_sent:   int = 0
    latencies_ms:     List[float] = field(default_factory=list)
    total_time_s:     float = 0.0

    @property
    def delivery_rate(self) -> float:
        return (self.packets_received / self.packets_sent * 100
                if self.packets_sent else 0.0)

    @property
    def avg_latency_ms(self) -> float:
        return (sum(self.latencies_ms) / len(self.latencies_ms)
                if self.latencies_ms else 0.0)

    @property
    def overhead_pct(self) -> float:
        base = self.packets_sent * (_HDR + PAYLOAD_SIZE)
        extra = (self.retransmissions * (_HDR + PAYLOAD_SIZE)
                 + self.ack_bytes_sent)
        return extra / base * 100 if base else 0.0


# ── Shared configuration ──────────────────────────────────────────────────────

PAYLOAD_SIZE = 64          # bytes of dummy payload per packet
_BASE_PORT   = 15000       # first UDP port to use (avoids conflicts)


# ── Lossy receiver helper ─────────────────────────────────────────────────────

class LossyReceiver:
    """
    Wraps a UDP socket and artificially drops packets with probability
    `loss_rate` (0.0 – 1.0).  Received (non-dropped) data is put into
    a thread-safe list.
    """
    def __init__(self, sock: socket.socket, loss_rate: float) -> None:
        self.sock      = sock
        self.loss_rate = loss_rate
        self.received  : List[tuple] = []   # list of (data, addr)
        self._lock     = threading.Lock()
        self._stop     = threading.Event()
        self._thread   = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        self.sock.settimeout(0.1)
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if random.random() >= self.loss_rate:   # keep packet
                with self._lock:
                    self.received.append((data, addr))

    def get_all(self) -> List[tuple]:
        with self._lock:
            items = list(self.received)
            self.received.clear()
        return items

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1)


# ── Experiment 1: Best-Effort UDP ────────────────────────────────────────────

def run_best_effort(
    num_packets: int,
    loss_rate:   float,
    send_port:   int,
    recv_port:   int,
) -> ExperimentResult:
    """
    Sender fires `num_packets` datagrams with no ACK and no retransmit.
    Receiver counts how many arrive (through artificial loss filter).
    """
    res = ExperimentResult(mode="Best-Effort UDP")
    res.packets_sent = num_packets

    recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    recv_sock.bind(("127.0.0.1", recv_port))

    send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    send_sock.bind(("127.0.0.1", send_port))

    lossy = LossyReceiver(recv_sock, loss_rate)

    payload = os.urandom(PAYLOAD_SIZE)
    dst     = ("127.0.0.1", recv_port)

    start = time.perf_counter()
    for seq in range(1, num_packets + 1):
        pkt = _pack(MSG_DATA, seq, payload)
        send_sock.sendto(pkt, dst)
        res.total_bytes_sent += len(pkt)

    # Give the receiver a moment to drain in-flight packets
    time.sleep(0.2)

    res.total_time_s  = time.perf_counter() - start
    received = lossy.get_all()
    res.packets_received = len(received)

    lossy.stop()
    send_sock.close()
    recv_sock.close()
    return res


# ── Experiment 2: Reliable UDP ────────────────────────────────────────────────

def run_reliable(
    num_packets:  int,
    loss_rate:    float,
    ack_timeout:  float,
    max_retries:  int,
    send_port:    int,
    recv_port:    int,
) -> ExperimentResult:
    """
    Sender transmits one packet at a time and waits for an ACK.
    If the ACK doesn't arrive within `ack_timeout` seconds it retransmits
    (up to `max_retries` times).
    Receiver echoes an ACK for every received packet.

    This mirrors the ACK+retransmit loop in server.py / client.py.
    """
    res = ExperimentResult(mode="Reliable UDP")
    res.packets_sent = num_packets

    # Receiver socket (acts as the "client")
    recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    recv_sock.bind(("127.0.0.1", recv_port))
    recv_sock.settimeout(0.05)

    # Sender socket (acts as the "server")
    send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    send_sock.bind(("127.0.0.1", send_port))
    send_sock.settimeout(ack_timeout)

    recv_addr  = ("127.0.0.1", recv_port)
    send_addr  = ("127.0.0.1", send_port)
    payload    = os.urandom(PAYLOAD_SIZE)
    seen_seqs  = set()

    # Receiver thread: read DATA, send ACK back (also through loss filter)
    recv_stop  = threading.Event()
    recv_count = [0]

    def receiver_loop() -> None:
        while not recv_stop.is_set():
            try:
                data, addr = recv_sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            # Artificial loss on incoming DATA packets
            if random.random() < loss_rate:
                continue
            msg_type, seq, _ = _unpack(data)
            if msg_type != MSG_DATA:
                continue
            if seq not in seen_seqs:
                seen_seqs.add(seq)
                recv_count[0] += 1
            # Send ACK (also subject to loss on its return trip)
            ack = _pack(MSG_ACK, seq)
            if random.random() >= loss_rate:   # ACK loss too
                try:
                    recv_sock.sendto(ack, addr)
                    res.ack_bytes_sent += len(ack)
                except OSError:
                    pass

    recv_thread = threading.Thread(target=receiver_loop, daemon=True)
    recv_thread.start()

    start = time.perf_counter()

    for seq in range(1, num_packets + 1):
        pkt      = _pack(MSG_DATA, seq, payload)
        delivered = False
        t_send   = time.perf_counter()

        for attempt in range(max_retries + 1):
            if attempt > 0:
                res.retransmissions += 1
            send_sock.sendto(pkt, recv_addr)
            res.total_bytes_sent += len(pkt)

            # Wait for ACK
            deadline = time.perf_counter() + ack_timeout
            while time.perf_counter() < deadline:
                try:
                    ack_data, _ = send_sock.recvfrom(256)
                except socket.timeout:
                    break
                msg_type, ack_seq, _ = _unpack(ack_data)
                if msg_type == MSG_ACK and ack_seq == seq:
                    delivered = True
                    break
            if delivered:
                break

        if delivered:
            res.latencies_ms.append((time.perf_counter() - t_send) * 1000)
            res.packets_received += 1

    res.total_time_s = time.perf_counter() - start

    recv_stop.set()
    recv_thread.join(timeout=1)
    send_sock.close()
    recv_sock.close()
    return res


# ── Report ────────────────────────────────────────────────────────────────────

def print_report(be: ExperimentResult, rel: ExperimentResult,
                 loss_rate: float) -> None:
    W = 62
    sep = "─" * W

    def row(label: str, be_val: str, rel_val: str) -> None:
        print(f"  {label:<28}  {be_val:>12}  {rel_val:>12}")

    print()
    print("=" * W)
    print("  PERFORMANCE COMPARISON: Reliable UDP vs Best-Effort UDP")
    print("=" * W)
    print(f"  Simulated network loss rate : {loss_rate*100:.0f}%")
    print(f"  Packets per experiment      : {be.packets_sent}")
    print(sep)
    print(f"  {'Metric':<28}  {'Best-Effort':>12}  {'Reliable':>12}")
    print(sep)
    row("Packets sent (incl. rexmit)",
        str(be.packets_sent),
        str(rel.packets_sent + rel.retransmissions))
    row("Packets received",
        str(be.packets_received),
        str(rel.packets_received))
    row("Delivery rate",
        f"{be.delivery_rate:.1f}%",
        f"{rel.delivery_rate:.1f}%")
    row("Retransmissions",
        "N/A",
        str(rel.retransmissions))
    row("Avg latency (ms)",
        "N/A",
        f"{rel.avg_latency_ms:.2f} ms")
    row("Total wall-clock time (s)",
        f"{be.total_time_s:.3f} s",
        f"{rel.total_time_s:.3f} s")
    row("Bandwidth overhead",
        "0.0%",
        f"{rel.overhead_pct:.1f}%")
    print(sep)
    print()
    print("  CONCLUSION")
    print(sep)
    gain = rel.delivery_rate - be.delivery_rate
    print(f"  Reliable UDP delivers {gain:+.1f} pp more packets than "
          f"best-effort")
    print(f"  at the cost of {rel.overhead_pct:.1f}% extra bandwidth and "
          f"{rel.total_time_s - be.total_time_s:.3f}s extra time.")
    print()
    print("  This demonstrates why custom reliability (ACKs + retransmit +")
    print("  sequence numbers) is necessary for group notification delivery")
    print("  over an unreliable UDP channel.")
    print("=" * W)
    print()


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(
        description="Reliable vs Best-Effort UDP performance comparison")
    p.add_argument("--packets",     type=int,   default=100,
                   help="Number of data packets per experiment (default: 100)")
    p.add_argument("--loss",        type=float, default=15.0,
                   help="Simulated packet-loss percentage (default: 15.0)")
    p.add_argument("--timeout",     type=float, default=0.2,
                   help="ACK timeout in seconds for reliable mode (default: 0.2)")
    p.add_argument("--max-retries", type=int,   default=5,
                   help="Max retransmit attempts (default: 5)")
    p.add_argument("--seed",        type=int,   default=None,
                   help="Random seed for reproducible loss simulation")
    args = p.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    loss = args.loss / 100.0
    if not (0.0 <= loss < 1.0):
        p.error("--loss must be between 0 and 99")

    print(f"\n  Running Best-Effort experiment  ({args.packets} packets, "
          f"{args.loss:.0f}% loss) …")
    be_result = run_best_effort(
        num_packets=args.packets,
        loss_rate=loss,
        send_port=_BASE_PORT,
        recv_port=_BASE_PORT + 1,
    )

    # Brief pause so OS recycles ports
    time.sleep(0.3)

    print(f"  Running Reliable    experiment  ({args.packets} packets, "
          f"{args.loss:.0f}% loss) …")
    rel_result = run_reliable(
        num_packets=args.packets,
        loss_rate=loss,
        ack_timeout=args.timeout,
        max_retries=args.max_retries,
        send_port=_BASE_PORT + 2,
        recv_port=_BASE_PORT + 3,
    )

    print_report(be_result, rel_result, loss)


if __name__ == "__main__":
    main()
