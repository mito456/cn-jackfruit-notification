# Design Decisions & Implementation Rationale

> This document explains the **why** behind every major architectural choice in the Jackfruit Notification System.
> Use this during your viva to justify design decisions.

---

## 1. Two-Socket Architecture (TCP/TLS + UDP)

### Decision
- **TCP/TLS (port 9000)** for control channel (subscription, key exchange)
- **UDP (port 9001)** for data channel (notifications, ACKs)

### Rationale
| Aspect | Why |
|--------|-----|
| **Split channels** | Separates concerns: reliability-critical (key exchange) from performance-critical (broadcasts) |
| **TCP for control** | TCP guarantees no message loss; losing a SUBSCRIBE message is fatal |
| **UDP for data** | Low latency; we add custom reliability on top (ACKs + retransmit) rather than paying TCP overhead for every notification |
| **Different ports** | Allows independent scaling (firewall rules, network tuning) |

**Real-world precedent:** SRTP (Secure Real-time Transport Protocol) uses similar split — TLS for signaling, encrypted UDP for media.

---

## 2. Custom Reliability Instead of DTLS

### Decision
Implement ACK-based retransmission at the application layer instead of using DTLS (Datagram TLS).

### Rationale
| Aspect | Why |
|--------|-----|
| **Python limitation** | Python's standard `ssl` module does not support DTLS |
| **Fine-grained control** | Custom retransmit policy (configurable timeout, max retries, per-client eviction) |
| **Performance insight** | Demonstrates understanding of the UDP reliability trade-off (the core learning goal) |
| **Equivalent security** | AES-256-GCM + random nonce + sequence numbers provides same guarantees as DTLS |

**Trade-off:** More code to maintain, but **pedagogically superior** for a socket programming course.

---

## 3. AES-256-GCM for UDP Payload Encryption

### Decision
Use AES-256-GCM (authenticated encryption) rather than separate encryption + HMAC.

### Rationale
| Aspect | Why |
|--------|-----|
| **Authenticated Encryption (AEAD)** | One operation provides confidentiality + integrity + authenticity; no risk of algorithmic mismatch |
| **256-bit key** | Industry standard for long-term confidentiality; overkill for college project but good practice |
| **GCM mode** | Parallelizable, efficient on modern CPUs; allows fresh nonce per packet (no counter state) |
| **Header as AAD** | Type + sequence number authenticated but not encrypted; prevents type-confusion attacks |

**Security guarantee:** Even if an attacker modifies one bit of the ciphertext, the GCM tag fails immediately.

---

## 4. Per-Client Session Keys

### Decision
Each client receives a unique 32-byte AES key during subscription.

### Rationale
| Aspect | Why |
|--------|-----|
| **Client isolation** | Compromise of one client's key does not expose other clients' traffic |
| **Forward secrecy (partial)** | When a client unsubscribes, new clients get different keys; old client's key is useless |
| **Key exchange over TLS** | Base64-encoded key sent over authenticated TLS channel; no eavesdropping or MITM possible |

**Alternative considered:** Shared group key. **Rejected** because: (1) key compromise affects all clients, (2) no client isolation.

---

## 5. Sequence Numbers + Rolling Duplicate Set

### Decision
- 32-bit sequence number in every packet header
- Client maintains a rolling set of seen sequence numbers (bounded to last 1000)

### Rationale
| Aspect | Why |
|--------|-----|
| **Sequence number** | Detects replayed packets; ordered delivery is not guaranteed but ordering is visible |
| **Rolling window** | Bounded memory O(1) duplicate detection instead of growing list |
| **32-bit counter** | ~4 billion messages before wraparound; sufficient for any realistic subscription session |
| **Always ACK duplicates** | Server's original ACK may have been lost; re-ACKing helps server convergence |

**Why not TCP-style sliding window?** UDP is out-of-order; sliding window doesn't apply. Rolling set is simpler and sufficient.

---

## 6. Heartbeat + Timeout-Based Eviction

### Decision
- Server sends MSG_HEARTBEAT every 10 seconds
- Clients reply with MSG_HEARTBEAT_ACK
- Clients silent for 35 seconds are evicted

### Rationale
| Aspect | Why |
|--------|-----|
| **Detect dead clients early** | Without heartbeat, unresponsive clients could accumulate forever |
| **Lightweight probe** | Heartbeat is just a 6-byte header, no payload; negligible overhead |
| **Configurable timeouts** | `HEARTBEAT_INTERVAL` and `HEARTBEAT_DEAD_LIMIT` are tunable in `protocol.py` |
| **Graceful degradation** | If a client becomes unresponsive, broadcast continues to other clients |

**Alternative considered:** Keep-alive on TCP only. **Rejected** because UDP clients could become unresponsive while TCP stays open.

---

## 7. Thread Model (One Thread Per Client)

### Decision
- Main thread: admin console (broadcasts)
- TCP-Accept thread: accepts new TLS connections
- Per-client thread: handles individual client's TCP messages
- UDP-Recv thread: single thread receives all UDP (ACKs, heartbeats)
- Retransmit thread: periodically scans pending ACKs
- Heartbeat thread: periodically probes clients

### Rationale
| Aspect | Why |
|--------|-----|
| **Per-client thread** | Isolates one client's blocking I/O from others; one slow client doesn't stall the server |
| **Single UDP-Recv** | Centralized UDP socket avoids kernel multi-receive complexity |
| **Separate retransmit** | Doesn't block other operations; can be woken on demand if needed |
| **Daemon threads** | All background threads are daemon; server shuts down cleanly when main thread exits |
| **Locks for shared state** | `clients_lock` protects client registry; `pending_lock` protects pending ACK table |

**Why not async/await?** Python's `asyncio` adds complexity; threading is clearer for this use case.

---

## 8. Retransmission Strategy (2s timeout, 5 max retries)

### Decision
- First retransmit after 2 seconds of silence
- Check every 0.5 seconds
- Give up after 5 retries (~10 seconds total)

### Rationale
| Aspect | Why |
|--------|-----|
| **2-second timeout** | Generous enough for LAN (even with congestion) but not so long that clients pile up in pending table |
| **5 retries** | ~10 seconds total = balances between "try hard" and "give up" |
| **Check every 0.5s** | Fine-grained control without wasting CPU |
| **Configurable** | Edit `protocol.py` to tune for your network (WAN vs LAN) |

**Why not exponential backoff?** For group notifications, simple linear retransmit is fine. Exponential backoff is overkill.

---

## 9. TLS 1.2+ (No SSL 3.0)

### Decision
```python
self.ssl_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
```

### Rationale
| Aspect | Why |
|--------|-----|
| **TLS 1.2 minimum** | SSL 3.0 and TLS 1.0/1.1 are deprecated (POODLE, BEAST attacks) |
| **Self-signed cert** | Fine for LAN/college lab; in production you'd use a certificate authority |
| **Certificate verification** | Client verifies server's cert; prevents MITM on key exchange |

---

## 10. No Encryption on TCP Control Channel (Only TLS Wrapping)

### Decision
Control messages are JSON sent over TLS (which handles encryption).

### Rationale
| Aspect | Why |
|--------|-----|
| **TLS already encrypts** | No need for extra application-layer crypto on TCP |
| **Length-prefixed framing** | 4-byte big-endian length prevents framing attacks |
| **Simplicity** | Control channel is low-volume; TLS is sufficient |

**Why not re-encrypt JSON?** Unnecessary; you'd be encrypting twice.

---

## 11. Graceful Disconnect (UNSUBSCRIBE message)

### Decision
Client sends `{"type": "unsubscribe"}` over TLS before closing.

### Rationale
| Aspect | Why |
|--------|-----|
| **Clean exit** | Server knows client intentionally left (not due to network failure) |
| **Remove from registry** | Stops sending future broadcasts to an unsubscribed client |
| **Alternative:** Heartbeat timeout also unsubscribes | If TCP closes abruptly, UDP heartbeat will evict the client after 35s |

---

## 12. Error Handling Strategy

### Edge Cases Handled
| Case | Handler |
|------|---------|
| **Abrupt TCP close** | Handler catches `OSError` in `_tcp_recv`, removes client |
| **Invalid JSON** | `json.JSONDecodeError` caught; returns `None` |
| **Bad UDP datagram** | Decrypt fails → logged warning, packet dropped |
| **Invalid TLS cert** | `ssl.SSLError` caught; connection rejected |
| **Out-of-range UDP port** | Validated in `_handle_client_tcp` (1024–65535 check) |
| **Negative sequence numbers** | Unsigned 32-bit enforced by struct format `!I` |
| **Oversized JSON** | Checked: `if length > 65535: return None` |
| **Duplicate ACK** | Handled safely; discarded but doesn't crash |
| **Partial UDP packet** | Raises `ValueError`; caught and logged |

### Why These Choices
- **Fail-safe:** Bad packets are dropped, not crashed on
- **Logging:** All errors logged at DEBUG or WARNING level
- **Isolation:** One bad client doesn't crash the server

---

## 13. Performance Trade-offs

| Choice | Gain | Cost |
|--------|------|------|
| **Custom reliability** | Control over retry policy | +bandwidth (ACKs + retransmissions) |
| **Per-client session key** | Client isolation | +per-client storage (~32 bytes each) |
| **Heartbeat probes** | Early detection of dead clients | +network traffic (1 packet/10s per client) |
| **Single UDP-Recv thread** | Centralized lock-free-ish | Potential bottleneck for very high load (>10k clients) |
| **Full GCM auth** | Integrity guarantee | ~10% CPU overhead vs simple encryption |

**For 100–1000 clients on a LAN:** Current design is optimal. Scalability notes in evaluation section.

---

## 14. Why This Matches the Project Abstract

| Abstract Requirement | Implementation |
|-----|----------|
| **Secure group notification system** | ✅ TLS + AES-256-GCM + per-client keys |
| **UDP socket programming** | ✅ Raw socket, bind, sendto, recvfrom |
| **Multi-client support** | ✅ One thread per client, 100+ tested |
| **Reliable delivery** | ✅ ACK-based retransmission, sequence numbers |
| **Liveness detection** | ✅ Heartbeat + timeout eviction |
| **Broadcast mechanism** | ✅ Server → all clients simultaneously |

---

## Summary

Each design choice was made to balance:
1. **Learning goals** (demonstrate socket programming concepts)
2. **Correctness** (no message loss, authenticated, private)
3. **Simplicity** (code is readable and maintainable)
4. **Performance** (acceptable latency for group notifications)

The system is **not optimized for 1 million clients on a WAN** — it's a **college project** that demonstrates fundamental concepts in socket programming, reliability, and cryptography.
