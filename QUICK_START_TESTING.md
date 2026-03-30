# Quick Start: Testing & Evaluation

> Run this checklist to test everything and prepare for viva.

---

## Prerequisites

- ✅ Python 3.9+
- ✅ Dependencies installed: `pip install -r requirements.txt`
- ✅ Certificates generated: `python generate_certs.py`
- ✅ Code on GitHub (you already did this)

---

## 1️⃣ Baseline Test (5 minutes)

### Open 2 terminals:

**Terminal 1 — Start server:**
```bash
python server.py
```

**Terminal 2 — Start one client:**
```bash
python client.py Alice
```

**Terminal 1 — Send a message:**
```
Hello from the server!
```

**Expected:**
- Terminal 2 shows notification with timestamp
- No errors in Terminal 1
- Server shows: `Broadcast #1 to 1 client(s): 'Hello from the server!'`

✅ **If this works, socket programming is correct.**

---

## 2️⃣ Performance Comparison (3 minutes)

**Terminal 2 — Stop client (Ctrl+C)**

**Terminal 2 — Run performance test:**
```bash
python performance_test.py --packets 100 --loss 15 --seed 42
```

**Expected output:**
```
  Reliable UDP delivers +X.X pp more packets than best-effort
  at the cost of Y.Y% extra bandwidth
```

**Note the numbers:**
- Reliable delivery rate should be close to 100%
- Best-effort should be ~85% (due to 15% loss)

✅ **This demonstrates custom reliability.**

---

## 3️⃣ Scalability Test — Small (5 minutes)

**Terminal 1 — Keep server running**

**Terminal 2 — Run stress test with 10 clients:**
```bash
python stress_test.py --clients 10 --broadcasts 50 --delay 0.05
```

**Expected output:**
```
  Clients actually connected: 10/10
  Delivery rate: 100.0%
  Avg latency: ~50-100ms
  Total errors: 0
```

**Record this:**
- Delivery rate: ____%
- Avg latency: ____ms
- P99 latency: ____ms

---

## 4️⃣ Scalability Test — Medium (10 minutes)

**Terminal 2 — Run with 50 clients:**
```bash
python stress_test.py --clients 50 --broadcasts 100 --delay 0.02
```

**Expected:**
- Delivery rate: ≥98%
- P99 latency: <300ms
- Errors: 0

**Record this:**
- Delivery rate: ____%
- P99 latency: ____ms
- Max latency: ____ms

---

## 5️⃣ Scalability Test — Heavy (15 minutes)

**Terminal 2 — Run with 100 clients:**
```bash
python stress_test.py --clients 100 --broadcasts 100 --delay 0.01
```

**Expected:**
- Delivery rate: ≥95% (may drop due to high load)
- P99 latency: <500ms
- Some errors acceptable (network contention)

**Record this:**
- Delivery rate: ____%
- P99 latency: ____ms
- Total errors: ____

---

## 6️⃣ Edge Case Testing (10 minutes)

### Test A: Abrupt Client Disconnect

**Terminal 1 — Keep server running**

**Terminal 2 — Start client A:**
```bash
python client.py Alice
```

**Terminal 3 — Start client B:**
```bash
python client.py Bob
```

**Terminal 1 — Send a message:**
```
Message 1
```

**Expected:** Both clients receive the message ✅

**Terminal 2 — Kill client A (Ctrl+C)**

**Terminal 1 — Send another message:**
```
Message 2
```

**Expected:**
- Only Bob receives Message 2
- Server logs: `Client 'Alice' disconnected`
- No server crash ✅

### Test B: SSL Handshake with Invalid Cert

**Terminal 2 — Delete server.crt or rename it:**
```bash
mv server.crt server.crt.bak
```

**Terminal 2 — Try to connect:**
```bash
python client.py Charlie
```

**Expected:**
- Connection fails with SSL error ✅
- Server does not crash ✅
- Other clients (if any) remain unaffected ✅

**Terminal 2 — Restore cert:**
```bash
mv server.crt.bak server.crt
```

---

## 7️⃣ Fill in EVALUATION_REPORT.md

**Using your test results, fill in:**

1. **Test Environment** (section 1)
   - Processor, RAM, OS
   - Network type (LAN/WAN)

2. **Baseline Performance** (section 2)
   - From your 1-client test above

3. **Scalability Results** (sections 3–5)
   - Copy numbers from stress_test output

4. **Edge Cases** (section 6)
   - Document what you tested

5. **Observations** (section 11)
   - Write a paragraph: "The system performed well under load..."

---

## 8️⃣ Prepare Viva Talking Points

Using `DESIGN_DECISIONS.md`, answer these questions in your own words:

1. **"Why two channels (TCP + UDP) instead of one?"**
   - Answer: TCP for reliability (key exchange), UDP for speed (broadcasts)

2. **"How is the data encrypted?"**
   - Answer: AES-256-GCM — encrypts + authenticates + checks integrity

3. **"What happens if a client suddenly disconnects?"**
   - Answer: Server sends heartbeat every 10s; if no response for 35s, client is evicted

4. **"How do you handle packet loss?"**
   - Answer: Sequence numbers + ACK-based retransmission (tested in performance_test.py)

5. **"How many clients can it handle?"**
   - Answer: Tested up to 100 on LAN; bottleneck is single UDP-Recv thread

6. **"Why not use DTLS instead of custom reliability?"**
   - Answer: Python doesn't support DTLS; custom reliability demonstrates understanding of UDP reliability trade-offs

---

## 9️⃣ Final Checklist

- [ ] Baseline test passed (1 client, 1 message)
- [ ] Performance test shows reliability improvement
- [ ] Stress test 10 clients shows 100% delivery
- [ ] Stress test 50 clients shows ≥98% delivery  
- [ ] Stress test 100 clients shows ≥95% delivery
- [ ] Edge case tests passed (disconnect, SSL failure)
- [ ] EVALUATION_REPORT.md filled with your results
- [ ] All files committed to GitHub
- [ ] DESIGN_DECISIONS.md read and understood
- [ ] Viva talking points prepared

---

## 🎯 What to Show During Viva

### Demo
```bash
# Terminal 1: Start server
python server.py

# Terminal 2–3: Start 2–3 clients
python client.py Alice
python client.py Bob

# Terminal 1: Broadcast a message
Hello everyone!

# All clients receive notification
# Show: TLS encryption, AES-256-GCM, sequence numbers
```

### Performance Results
```bash
python performance_test.py --packets 100 --loss 20 --seed 42
# Show: Reliable 100%, Best-effort 80%, +20pp improvement

python stress_test.py --clients 50 --broadcasts 100
# Show: 50 concurrent clients, 100% delivery, <200ms p99 latency
```

### Design Explanation
**Open `DESIGN_DECISIONS.md`** and walk through:
- Architecture (section 1–4)
- Reliability (section 8)
- Security (section 3, 4, 9)

---

## 📝 Sample Viva Script

> Use this as a template for your presentation:

---

**"Thank you. My project is the Jackfruit Group Notification System using socket programming.**

**Architecture:**  
I designed a two-channel system:
- **TCP/TLS (port 9000):** For subscription and key exchange — must be reliable
- **UDP (port 9001):** For broadcasts — fast, but I add reliability on top

**Reliability:**  
Since UDP loses packets, I implemented:
- Sequence numbers to detect duplicates
- ACK-based retransmission (client confirms receipt)
- Heartbeat probes to detect dead clients
- Configurable timeouts (2 seconds, 5 retries)

**Security:**  
- TLS 1.2+ for control channel (prevents MITM on key exchange)
- AES-256-GCM for message encryption (confidentiality + integrity)
- Per-client session keys (isolates clients)
- GCM authentication prevents tampering

**Performance:**  
I tested scalability:
- [Your results] — 20 clients, 100% delivery, 50ms latency
- [Your results] — 50 clients, 99% delivery, 150ms p99 latency  
- [Your results] — 100 clients, 95% delivery, 400ms p99 latency

**Trade-offs:**  
Custom reliability adds ~15% bandwidth overhead (ACKs + retransmissions) but guarantees delivery.

**Questions?**"

---

**Good luck! 🎓**
