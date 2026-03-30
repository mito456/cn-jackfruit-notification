# Jackfruit Group Notification System

> **CN Mini Project – Socket Programming**
> Secure, reliable, multi-client group notifications over UDP

---

## 1. Problem Statement

Design and implement a **secure group notification system** using UDP socket programming. A central server maintains a list of subscribed clients and broadcasts notifications to all of them simultaneously. Because UDP is inherently unreliable, the system implements custom reliability on top.

**Objectives**

| # | Objective |
|---|-----------|
| 1 | Multi-client subscription/unsubscription via a secure control channel |
| 2 | Fast broadcast delivery using UDP datagrams |
| 3 | Reliable delivery through ACKs, sequence numbers, and retransmission |
| 4 | Confidentiality, integrity, and authentication of every datagram |
| 5 | Liveness detection via heartbeats |

---

## 2. System Architecture

```
                        ┌─────────────────────────────────────┐
                        │         NOTIFICATION SERVER          │
                        │                                      │
                        │  ┌──────────────┐  ┌─────────────┐  │
TCP/TLS port 9000 ──────┼──│ Control Chan │  │  UDP Socket │──┼──── UDP port 9001
                        │  │ (subscribe / │  │ (broadcast  │  │
                        │  │  key exchange│  │  + retran.) │  │
                        │  └──────┬───────┘  └──────┬──────┘  │
                        │         │                  │         │
                        │  ┌──────▼──────────────────▼──────┐  │
                        │  │         Client Registry        │  │
                        │  │  {udp_addr → (name, AES key)}  │  │
                        │  └────────────────────────────────┘  │
                        └──────────────┬──────────────────────┘
                                       │
              ┌────────────────────────┼────────────────────────┐
              │                        │                        │
    ┌─────────▼──────┐      ┌──────────▼──────┐      ┌─────────▼──────┐
    │   CLIENT A     │      │   CLIENT B      │      │   CLIENT C     │
    │                │      │                 │      │                │
    │ TLS subscribe  │      │ TLS subscribe   │      │ TLS subscribe  │
    │ UDP listener   │      │ UDP listener    │      │ UDP listener   │
    │ ACK sender     │      │ ACK sender      │      │ ACK sender     │
    └────────────────┘      └─────────────────┘      └────────────────┘
```

### Component Roles

| Component | Role |
|-----------|------|
| **TCP/TLS Control Channel** | Client registers UDP port; server delivers unique AES-256 session key over an encrypted channel |
| **UDP Data Channel** | Server broadcasts encrypted notifications; clients send ACKs back |
| **Client Registry** | In-memory dict mapping `(ip, udp_port)` → `(name, session_key)` |
| **Pending ACK Table** | Per-broadcast set of clients that have not yet acknowledged |
| **Retransmit Thread** | Scans pending table every 0.5 s; resends after 2 s silence (up to 5 attempts) |
| **Heartbeat Thread** | Sends MSG_HEARTBEAT every 10 s; evicts silent clients after 35 s |

---

## 3. Protocol Design

### 3.1 TCP Control Channel (JSON over TLS)

Length-prefixed framing: **4-byte big-endian length** followed by UTF-8 JSON.

```
Subscribe (Client → Server)
  { "type": "subscribe", "client_name": "<name>", "udp_port": <int> }

Sub-ACK   (Server → Client)
  { "type": "sub_ack", "session_key": "<base64-AES-256>", "server_udp_port": <int> }

Unsubscribe (Client → Server)
  { "type": "unsubscribe" }
```

### 3.2 UDP Data Channel (Binary, Encrypted)

```
 Byte offset   0        1        2        3        4        5
               ┌────────┬────────┬────────────────────────────┐
 HEADER (6 B)  │VERSION │ TYPE   │      SEQUENCE NUMBER       │
 (plaintext,   │  0x01  │ 0x01…  │     (32-bit big-endian)    │
  used as AAD) └────────┴────────┴────────────────────────────┘

               ┌───────────────────────────────────────────────┐
 NONCE (12 B)  │          Random 96-bit nonce (AES-GCM)        │
               └───────────────────────────────────────────────┘

               ┌───────────────────────────────────────────────┐
 CIPHERTEXT    │  AES-256-GCM(payload) + 16-byte GCM tag       │
 + TAG         │  (tag authenticates header + payload)         │
               └───────────────────────────────────────────────┘
```

**Message Types**

| Hex  | Name           | Direction       | Payload |
|------|----------------|-----------------|---------|
| 0x01 | NOTIFICATION   | Server → Client | UTF-8 message text |
| 0x02 | ACK            | Client → Server | empty |
| 0x03 | HEARTBEAT      | Server → Client | empty |
| 0x04 | HEARTBEAT_ACK  | Client → Server | empty |

### 3.3 Sequence Number Flow

```
Server                               Client A                Client B
  │                                      │                       │
  │──── NOTIFICATION #7 ────────────────►│                       │
  │──── NOTIFICATION #7 ──────────────────────────────────────►  │
  │                                      │                       │
  │◄─── ACK #7 ─────────────────────────│                       │
  │                                      │                       │
  │  (no ACK from B within 2 s)          │                       │
  │──── NOTIFICATION #7 [retry-1] ────────────────────────────►  │
  │◄─── ACK #7 ──────────────────────────────────────────────────│
  │                                      │                       │
  ▼  (both clients ACKed, remove #7)     ▼                       ▼
```

---

## 4. Security Implementation

### 4.1 Two-Layer Security Model

```
Layer 1 – KEY EXCHANGE (TLS 1.2+)
  TCP socket wrapped with ssl.SSLContext
  Server certificate verified by client (self-signed CA)
  Prevents man-in-the-middle on session key delivery

Layer 2 – DATA ENCRYPTION (AES-256-GCM)
  Each client gets a unique 32-byte session key via Layer 1
  Every UDP datagram is independently encrypted + authenticated
  Nonce: fresh os.urandom(12) per packet ← prevents encryption reuse
  AAD:   6-byte packet header ← binds type/seq to ciphertext
```

### 4.2 Security Properties

| Property | Mechanism |
|----------|-----------|
| **Confidentiality** | AES-256 encryption (GCM mode) hides payload from eavesdroppers |
| **Integrity** | 16-byte GCM authentication tag – any bit flip is detected instantly |
| **Authenticity** | Only the key holder can produce a valid tag; forged packets are dropped |
| **Replay resistance** | Fresh nonce per packet + sequence number checked by client |
| **Forward secrecy (partial)** | Unique session key per client subscription session |
| **Transport security** | TLS 1.2+ with certificate verification on the control channel |

### 4.3 Why Not DTLS Directly?

Python's standard `ssl` module does not support DTLS.
The chosen approach — TLS for key exchange, AES-256-GCM for UDP payloads — provides **equivalent security guarantees** and is the standard pattern used by protocols such as SRTP (used in WebRTC).

---

## 5. Reliability Mechanisms

| Mechanism | Implementation |
|-----------|----------------|
| **Sequence numbers** | 32-bit counter per server broadcast, included in every datagram header |
| **ACK** | Client sends `MSG_ACK{seq}` after every valid notification |
| **Retransmission** | `Retransmit` thread wakes every `CHECK_INTERVAL=0.5 s`; resends after `RETRANSMIT_TIMEOUT=2 s` |
| **Max retries** | After `MAX_RETRIES=5` failures the client is evicted from the subscriber list |
| **Timeout / eviction** | `Heartbeat` thread removes clients silent for `>35 s` |
| **Duplicate detection** | Client maintains a rolling set of seen sequence numbers; displays each message once but always re-sends ACK |

---

## 6. Project Structure

```
cn_jackfruit_problem/
├── generate_certs.py      # One-time setup: creates server.crt + server.key
├── protocol.py            # Wire format constants, pack/unpack helpers
├── security.py            # AES-256-GCM encrypt / decrypt
├── server.py              # Notification server (run once)
├── client.py              # Subscriber client (run N times)
├── performance_test.py    # Reliable vs Best-Effort UDP comparison
├── stress_test.py         # Scalability & concurrency testing (10–100+ clients)
├── requirements.txt       # Python dependencies
├── README.md              # This file
├── DESIGN_DECISIONS.md    # Design rationale for all architectural choices
├── EVALUATION_REPORT.md   # Template for performance metrics (fill this for viva)
└── Socket Programming-Guidelines_Rubrics.pdf
```

---

## 7. Setup

### 7.1 Prerequisites

- Python 3.9+
- pip

### 7.2 Install dependencies

```bash
pip install -r requirements.txt
```

### 7.3 Generate TLS certificate (first time only)

```bash
python generate_certs.py
```

This creates `server.crt` and `server.key` in the working directory.
Copy `server.crt` to any machine running `client.py` (it is the CA cert).

---

## 8. Usage

### Terminal 1 – Start the server

```bash
python server.py
```

Optional arguments:
```
--host      Bind address  (default: 0.0.0.0)
--tcp-port  TLS control   (default: 9000)
--udp-port  UDP data      (default: 9001)
--cert      Certificate   (default: server.crt)
--key       Private key   (default: server.key)
```

### Terminal 2, 3, … – Start clients

```bash
python client.py Alice
python client.py Bob   --host <server-ip>
python client.py Carol --host <server-ip> --cafile /path/to/server.crt
```

Optional arguments:
```
--host      Server hostname/IP  (default: localhost)
--tcp-port  Server TLS port     (default: 9000)
--udp-port  Local UDP port      (default: 0 = auto-assign)
--cafile    CA cert path        (default: server.crt)
```

### Server admin commands

Once the server is running, type at its prompt:

```
Hello everyone!      ← broadcasts to all subscribed clients
list                 ← show subscribed clients with stats
stats                ← show aggregate performance counters
quit                 ← gracefully shut down
```

---

## 9. Demo Walkthrough

```
# Shell 1
$ python generate_certs.py
[OK] Private key  → server.key
[OK] Certificate  → server.crt

$ python server.py
09:01:00  [INFO]  Server  TCP/TLS control  → 0.0.0.0:9000
09:01:00  [INFO]  Server  UDP notification → 0.0.0.0:9001
===========================================================
  Jackfruit Notification Server  –  READY
===========================================================

# Shell 2
$ python client.py Alice
09:01:05  [INFO]  Client[Alice]  TLS connected  localhost:9000  |  TLSv1.3
09:01:05  [INFO]  Client[Alice]  Subscribed – session key 32 bytes

# Shell 3
$ python client.py Bob
09:01:07  [INFO]  Client[Bob]  TLS connected  localhost:9000  |  TLSv1.3

# Back in Shell 1 – type a message:
System maintenance at 10 PM tonight

# Shell 2 output:
  ┌──────────────────────────────────────────────────────┐
  │  [09:01:12]  NOTIFICATION  #1                        │
  │  System maintenance at 10 PM tonight                 │
  └──────────────────────────────────────────────────────┘

# Shell 3 output:  (same)
```

---

---

## 12. Testing & Performance Evaluation

### Quick Tests

```bash
# 1. Performance: Reliable vs Best-Effort UDP
python performance_test.py --packets 100 --loss 15 --seed 42

# 2. Stress test: 10 concurrent clients
python stress_test.py --clients 10 --broadcasts 50 --delay 0.05

# 3. Heavy load: 50 clients, fast messages
python stress_test.py --clients 50 --broadcasts 100 --delay 0.01
```

### Full Evaluation Workflow

1. **Start the server** (one terminal)
   ```bash
   python server.py --host 10.30.200.204
   ```

2. **Run performance baseline** (another terminal)
   ```bash
   python performance_test.py --packets 100 --loss 10
   python performance_test.py --packets 100 --loss 20
   ```

3. **Run scalability tests**
   ```bash
   python stress_test.py --clients 10 --broadcasts 50 --delay 0.05
   python stress_test.py --clients 50 --broadcasts 100 --delay 0.02
   python stress_test.py --clients 100 --broadcasts 100 --delay 0.01
   ```

4. **Fill in EVALUATION_REPORT.md** with results
   - Copy metrics from test output
   - Add observations and bottlenecks
   - Prepare talking points for viva

### What Each Test Measures

| Test | Purpose | Key Metric |
|------|---------|-----------|
| `performance_test.py` | Compares custom reliability vs best-effort | Delivery rate gain |
| `stress_test.py --clients 10` | Baseline concurrency | Latency distribution |
| `stress_test.py --clients 50` | Moderate load | Delivery rate stability |
| `stress_test.py --clients 100+` | Breaking point | Max sustainable load |

### Performance Expectations

| Scenario | Expected Result |
|----------|-----------------|
| Single client, LAN | <50 ms latency, 100% delivery |
| 10 clients, LAN | <100 ms p99 latency, 99%+ delivery |
| 50 clients, LAN | <200 ms p99 latency, 98%+ delivery |
| 100 clients, LAN | <500 ms p99 latency, 95%+ delivery |
| With 15% simulated loss | +15–20 pp delivery improvement (reliable vs best-effort) |

---

## 13. Design Decisions & Architecture

📄 See **`DESIGN_DECISIONS.md`** for detailed explanation of:
- Why TCP/TLS + UDP (not DTLS)
- Why AES-256-GCM (not separate encryption + HMAC)
- Why per-client session keys
- Why heartbeat + timeout eviction
- Error handling for edge cases
- Performance trade-offs

Use this document to prepare for your viva presentation.

---

## 14. Documentation for Viva

### Files to Reference During Presentation

1. **`DESIGN_DECISIONS.md`** – Explain architectural choices
   - *"Why did you choose TCP for control channel?"*
   - *"How does encryption work?"*
   - *"What about security?"*

2. **`EVALUATION_REPORT.md`** – Show performance results
   - *"How many clients can it handle?"*
   - *"What's the latency?"*
   - *"Is it stable under load?"*

3. **`README.md`** → Sections 11–14 – Protocol design, security properties

4. **`performance_test.py` output** – Demonstrate custom reliability
   - *"See how ACK+retransmit improves delivery rate?"*

5. **`stress_test.py` output** – Show scalability
   - *"Here we tested 100 concurrent clients …"*

### Talking Points Template

**"Our system is a secure group notification protocol using sockets:"**

1. **Architecture:**
   - Two channels: TLS for subscription, UDP for broadcasts
   - Reason: reliability for control, speed for data

2. **Reliability:**
   - Sequence numbers + ACK-based retransmit
   - Handles packet loss (tested up to 20%)
   - Performance: trades latency for guaranteed delivery

3. **Security:**
   - TLS 1.2+ for key exchange
   - AES-256-GCM for message encryption + authentication
   - Per-client session keys

4. **Performance:**
   - [Cite your stress_test.py results]
   - Scales to 50–100 clients on a LAN
   - Latencies: median <100 ms, p99 <300 ms

5. **Edge Cases Handled:**
   - Abrupt client disconnect → evicted after heartbeat timeout
   - SSL handshake failure → connection rejected, other clients unaffected
   - Corrupted UDP → GCM tag fails, packet dropped
   - Invalid JSON → logged, connection continues


| Decision | Rationale |
|----------|-----------|
| TCP only for subscription | Reliable by nature; key exchange must not be lost |
| UDP for notifications | Low latency; custom reliability gives control over retransmit policy |
| Per-client session key | Compromise of one client's key does not expose other clients' traffic |
| GCM mode (AEAD) | Single primitive provides both encryption and authentication |
| Random 12-byte nonce | Standard for AES-GCM; `os.urandom` uses OS CSPRNG |
| Header as AAD | Binds seq-num and message-type to ciphertext; prevents type-confusion attacks |
| Rolling seq-number set | Bounded memory with O(1) duplicate check |

---

## 11. Performance Comparison: Reliable vs Best-Effort UDP

`performance_test.py` runs both modes **locally** (no server needed) and prints a
side-by-side comparison table.

### How it works

| Mode | Description |
|------|-------------|
| **Best-Effort** | Sender fires all packets and never retransmits; no ACK |
| **Reliable** | Sender waits for per-packet ACK; retransmits on timeout (mirrors `server.py`) |

Artificial packet-loss (configurable %) is injected at the receiver to simulate
a lossy network.

### Run

```bash
# Default: 100 packets, 15% simulated loss
python performance_test.py

# Custom: 200 packets, 25% loss, 0.3s ACK timeout
python performance_test.py --packets 200 --loss 25 --timeout 0.3

# Reproducible run (fix random seed)
python performance_test.py --loss 20 --seed 42
```

### Sample output

```
  Running Best-Effort experiment  (100 packets, 15% loss) …
  Running Reliable    experiment  (100 packets, 15% loss) …

══════════════════════════════════════════════════════════════
  PERFORMANCE COMPARISON: Reliable UDP vs Best-Effort UDP
══════════════════════════════════════════════════════════════
  Simulated network loss rate : 15%
  Packets per experiment      : 100
──────────────────────────────────────────────────────────────
  Metric                         Best-Effort     Reliable
──────────────────────────────────────────────────────────────
  Packets sent (incl. rexmit)            100          117
  Packets received                        85          100
  Delivery rate                        85.0%        100.0%
  Retransmissions                          N/A           17
  Avg latency (ms)                         N/A        12.34 ms
  Total wall-clock time (s)           0.201 s        2.847 s
  Bandwidth overhead                     0.0%        15.3%
──────────────────────────────────────────────────────────────

  CONCLUSION
──────────────────────────────────────────────────────────────
  Reliable UDP delivers +15.0 pp more packets than best-effort
  at the cost of 15.3% extra bandwidth and 2.646s extra time.
══════════════════════════════════════════════════════════════
```

**Key insight:** Best-effort UDP is faster but lossy; reliable UDP guarantees
delivery at the cost of retransmission overhead and latency — exactly the
trade-off this project is designed to demonstrate.
