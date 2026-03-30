# 📚 Project Documentation Index

> Complete reference for understanding, testing, and presenting the Jackfruit Notification System

---

## 📖 Core Documentation

### For Understanding the System

| Document | Length | Purpose |
|----------|--------|---------|
| **README.md** | 10 KB | System overview, protocol design, setup instructions |
| **DESIGN_DECISIONS.md** | 11 KB | Why each design choice was made (for viva preparation) |
| **protocol.py** (code + comments) | 1 KB | Wire format specification, binary packet layout |
| **security.py** (code + comments) | 3 KB | Encryption mechanism (AES-256-GCM explanation) |

**Read in this order for complete understanding:**
1. README.md (get the big picture)
2. README section 3 (protocol design)
3. README section 4 (security)
4. DESIGN_DECISIONS.md (understand the why)
5. Code comments in server.py, client.py

---

## 🧪 Testing & Evaluation

### For Running Tests

| Document | Purpose | Time |
|----------|---------|------|
| **QUICK_START_TESTING.md** | Step-by-step testing guide with expected results | 60 min |
| **EVALUATION_REPORT.md** | Template to fill with your test results | Fill as you test |
| **stress_test.py** | Test with multiple concurrent clients | Included in QUICK_START_TESTING.md |
| **performance_test.py** | Compare reliable vs best-effort UDP | Included in QUICK_START_TESTING.md |

**Testing workflow:**
```
1. Open QUICK_START_TESTING.md
2. Follow sections 1️⃣ through 6️⃣
3. Record results in EVALUATION_REPORT.md
4. Continue to section 7️⃣ (fill viva talking points)
```

---

## 🎓 Viva Preparation

### For Your Final Presentation

| Document | Section | Purpose |
|----------|---------|---------|
| **DESIGN_DECISIONS.md** | Sections 1–14 | Answer "why" questions |
| **QUICK_START_TESTING.md** | Section 8 | Sample viva script |
| **EVALUATION_REPORT.md** | All filled sections | Show performance data |
| **README.md** | Sections 1–5 | Explain architecture & protocol |
| **performance_test.py output** | N/A | Demonstrate reliability gain |
| **stress_test.py output** | N/A | Demonstrate scalability |

**Viva talking points (use DESIGN_DECISIONS.md):**
- "Why two channels?" → Section 1
- "How is it encrypted?" → Section 3
- "How does reliability work?" → Section 8
- "What about security?" → Sections 3, 4, 9
- "How many clients can it handle?" → Section 13
- "Why custom reliability instead of DTLS?" → Section 2

---

## 🏗️ Source Code Organization

### Understanding the Code

```
Core Functionality
├── server.py (567 lines)
│   ├── NotificationServer class
│   ├── _accept_loop() – accept TLS connections
│   ├── _handle_client_tcp() – per-client subscription handler
│   ├── _dispatch_udp() – receive ACKs and heartbeats
│   ├── _retransmit_loop() – resend unacknowledged messages
│   ├── _heartbeat_loop() – probe clients for liveness
│   └── broadcast() – send notification to all clients
│
├── client.py (343 lines)
│   ├── NotificationClient class
│   ├── connect() – subscribe over TLS, get session key
│   ├── _udp_recv_loop() – listen for broadcasts
│   ├── _dispatch_udp() – handle incoming messages
│   ├── _handle_notification() – display message, send ACK
│   └── disconnect() – graceful unsubscribe
│
Protocol & Security
├── protocol.py (104 lines)
│   ├── Packet format (header, nonce, ciphertext, tag)
│   ├── Message type constants
│   ├── Reliability parameters (timeouts, retries)
│   └── pack_header() & unpack_header() helpers
│
├── security.py (96 lines)
│   ├── AES-256-GCM encryption
│   ├── generate_session_key() – create unique key per client
│   ├── encrypt_message() – confidential + authenticated
│   └── decrypt_message() – verify + decrypt
│
Setup
├── generate_certs.py (102 lines)
│   └── Create self-signed TLS certificate and private key
│
Testing
├── performance_test.py (378 lines)
│   └── Compare reliable vs best-effort UDP
│
├── stress_test.py (450 lines)
│   └── Concurrent client testing, scalability analysis
```

---

## 📊 Quick Reference

### Key Concepts

| Concept | Implemented In | Purpose |
|---------|-----------------|---------|
| **Subscription** | client.py:connect(), server.py:_handle_client_tcp() | Join the notification group |
| **Broadcast** | server.py:broadcast() | Send message to all clients |
| **Reliability** | server.py:_retransmit_loop(), _dispatch_udp() | ACK + retransmit on timeout |
| **Encryption** | security.py:encrypt_message(), parse_udp_packet() | AES-256-GCM per packet |
| **Key Exchange** | client.py:connect(), server.py:_handle_client_tcp() | Per-client key over TLS |
| **Liveness** | server.py:_heartbeat_loop(), client.py:_send_heartbeat_ack() | Detect dead clients |

### Configuration Parameters

See **protocol.py** for tuning:

```python
MAX_RETRIES          = 5      # max retransmit attempts
RETRANSMIT_TIMEOUT   = 2.0    # seconds before first retransmit
CHECK_INTERVAL       = 0.5    # retransmit check frequency
HEARTBEAT_INTERVAL   = 10.0   # probe interval
HEARTBEAT_DEAD_LIMIT = 35.0   # seconds of silence → evict
```

---

## 🔍 Troubleshooting Guide

### Common Issues & Solutions

| Problem | Likely Cause | Solution |
|---------|--------------|----------|
| "Connection refused" | Server not running or wrong port | Check: `netstat -an \| grep 9000` |
| "Certificate verify failed" | Wrong server.crt on client | Regenerate: `python generate_certs.py` and copy |
| "Connection reset by peer" | TLS handshake mismatch | Ensure matching certs; see above |
| "No clients subscribed" | Clients can't connect to server | Check firewall; try `--host 0.0.0.0` on server |
| "Low delivery rate" | Simulated loss or network issues | Run `performance_test.py` to verify retransmit works |
| "High latency on slow network" | Expected for low-bandwidth links | Tune `RETRANSMIT_TIMEOUT` in protocol.py |
| "Server crashes" | Unhandled exception | Check log level in server.py; report error |

---

## 📋 Submission Checklist

### Before Final Submission

- [ ] All code works (baseline test passed)
- [ ] All tests run without crashing
- [ ] Performance tests show expected results
- [ ] EVALUATION_REPORT.md filled with your metrics
- [ ] DESIGN_DECISIONS.md reviewed and understood
- [ ] All files committed to GitHub
- [ ] GitHub README is readable and clear
- [ ] `.gitignore` excludes server.key (private key)
- [ ] Viva talking points prepared
- [ ] Demo walkthrough practiced (server + 2–3 clients)

---

## 🎯 Deliverable Mapping

### What the Rubric Wants → What You Have

| Rubric Section | Requirement | Delivered In |
|---|---|---|
| **Socket Programming** | Low-level socket handling | server.py, client.py (raw socket API) |
| **Multiple Clients** | Support concurrent subscriptions | stress_test.py demonstrates up to 100 |
| **SSL/Security** | Encrypted & authenticated | security.py (AES-256-GCM), server.py:ssl.SSLContext |
| **Functional Correctness** | Works end-to-end | QUICK_START_TESTING.md section 1 |
| **Optimization & Fixes** | Refined based on testing | Error handling documented in DESIGN_DECISIONS.md |
| **Edge Cases** | Handle failures gracefully | QUICK_START_TESTING.md sections 6A–6B |
| **Design Explanation** | Clear rationale | DESIGN_DECISIONS.md (all 14 sections) |
| **Performance Evaluation** | Metrics under realistic load | EVALUATION_REPORT.md (all sections) |
| **GitHub & Docs** | Well-documented code | README.md + 5 supplementary docs |

---

## 📱 File Reference

### All Files in the Project

```
Executable Scripts
├── generate_certs.py      – Create TLS certificate
├── server.py              – Main server (run once)
├── client.py              – Client subscriber (run N times)
├── stress_test.py         – Scalability testing
└── performance_test.py    – Reliability comparison

Core Modules
├── protocol.py            – Wire format & constants
├── security.py            – Encryption/decryption
└── requirements.txt       – Python dependencies

Documentation
├── README.md              – Complete project guide
├── DESIGN_DECISIONS.md    – Architecture rationale
├── EVALUATION_REPORT.md   – Performance template
├── QUICK_START_TESTING.md – Testing checklist
├── DELIVERABLE_2_SUMMARY.md – What was added
└── PROJECT_INDEX.md       – This file

Data Files
├── server.crt             – TLS certificate (public)
├── server.key             – TLS private key (secret)
└── Socket Programming-Guidelines_Rubrics.pdf – Rubric
```

---

## 🚀 Getting Started

### If You Just Cloned the Repo

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Generate certificates (if not present)
python generate_certs.py

# 3. Start the server
python server.py

# 4. In another terminal, start a client
python client.py Alice

# 5. On the server, type a message
Hello everyone!

# 6. You should see the notification on the client
```

### If You Want to Run Full Tests

```bash
# 1. Have server running (python server.py)

# 2. In new terminal
python performance_test.py --packets 100 --loss 15 --seed 42

# 3. In new terminal
python stress_test.py --clients 10 --broadcasts 50 --delay 0.05

# 4. Fill in EVALUATION_REPORT.md with results

# 5. Commit & push
git add . && git commit -m "Test results added" && git push
```

---

## 🎓 Quick Viva Answer Guide

**Q: What does your system do?**  
A: It's a secure group notification system using UDP for fast broadcasts, backed by custom reliability (ACKs + retransmit) and encryption (AES-256-GCM).

**Q: Why UDP and not TCP?**  
A: UDP is low-latency for broadcasting. We add reliability on top (sequence numbers + ACKs). Justification: performance vs control trade-off.

**Q: How is it encrypted?**  
A: TLS 1.2+ for key exchange (control channel), then AES-256-GCM for each UDP packet. See security.py.

**Q: How do you handle packet loss?**  
A: Per-packet sequence number + client ACK. If no ACK within 2 seconds, retransmit (up to 5 times).

**Q: What if a client crashes?**  
A: Heartbeat probe every 10 seconds. If no response for 35 seconds, client evicted from subscriber list.

**Q: How many clients can it handle?**  
A: Tested up to 100 on LAN; bottleneck is single UDP-Recv thread. Could scale further with epoll/async I/O.

**Q: Where's the code?**  
A: GitHub: [your repo]. All documented in README + DESIGN_DECISIONS.md.

---

## 📞 Support

- **Code questions:** See the docstrings in server.py, client.py
- **Design questions:** See DESIGN_DECISIONS.md
- **Test questions:** See QUICK_START_TESTING.md
- **Performance questions:** See EVALUATION_REPORT.md template + stress_test.py output
- **Viva prep:** Combine DESIGN_DECISIONS.md + your test results

---

**Good luck with your final submission! 🎓**
