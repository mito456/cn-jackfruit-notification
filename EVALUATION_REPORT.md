# Performance Evaluation Report

> This document captures performance metrics, test results, and observations under realistic conditions.
> Fill this in as you run tests and save before final submission.

---

## Executive Summary

**System:** Jackfruit Group Notification System  
**Test Date:** [YOUR DATE]  
**Tested Configuration:** [describe your test setup]  
**Overall Verdict:** [PASS/FAIL/MARGINAL]

---

## 1. Test Environment

### Hardware
```
Server Machine
  Processor:  [CPU model, cores]
  RAM:        [GB]
  Network:    [Ethernet / Wi-Fi]
  OS:         [Windows/Linux/Mac]

Client Machine(s)
  Processor:  [CPU model, cores]
  RAM:        [GB]
  Network:    [Ethernet / Wi-Fi]
  OS:         [Windows/Linux/Mac]

Network
  Topology:   [LAN / WAN / Mixed]
  Latency:    [typical ping time]
  Bandwidth:  [approximate speed]
  Packet Loss: [0% / simulated %]
```

### Software Versions
```
Python:      [version]
cryptography: [version]
OS kernel:   [version]
```

---

## 2. Baseline Performance (Single Client, Single Broadcast)

### Test: `python client.py Alice`  &  `python server.py` → send 1 message

| Metric | Value | Target | Status |
|--------|-------|--------|--------|
| Subscription time (ms) | ___ | < 100 | ✓/✗ |
| Message latency (ms) | ___ | < 50 | ✓/✗ |
| Memory per client (MB) | ___ | < 5 | ✓/✗ |
| CPU usage (%) | ___ | < 10 | ✓/✗ |

**Observations:**
```
[Describe what you saw]
```

---

## 3. Scalability Test (Multiple Concurrent Clients)

### Test: `python stress_test.py --clients 10 --broadcasts 50 --delay 0.05`

#### Scenario A: 10 Clients
```
Run Command:  python stress_test.py --clients 10 --broadcasts 50 --delay 0.05
Date/Time:    [when you ran it]

Results:
  Clients connected:         ___/10
  Delivery rate:             ___ %
  Min/Avg/P99 latency (ms):  ___ / ___ / ___
  Total test time (s):       ___
  Errors:                    ___
  
Observations:
  [Was it stable? Did all clients connect? Any errors?]
```

#### Scenario B: 50 Clients
```
Run Command:  python stress_test.py --clients 50 --broadcasts 100 --delay 0.05
Date/Time:    [when you ran it]

Results:
  Clients connected:         ___/50
  Delivery rate:             ___ %
  Min/Avg/P99 latency (ms):  ___ / ___ / ___
  Total test time (s):       ___
  Errors:                    ___
  
Observations:
  [Did latency degrade? Any connection failures?]
```

#### Scenario C: 100 Clients
```
Run Command:  python stress_test.py --clients 100 --broadcasts 100 --delay 0.02
Date/Time:    [when you ran it]

Results:
  Clients connected:         ___/100
  Delivery rate:             ___ %
  Min/Avg/P99 latency (ms):  ___ / ___ / ___
  Total test time (s):       ___
  Errors:                    ___
  
Observations:
  [Breaking point? Performance cliff?]
```

---

## 4. High-Load Test (Fast Message Rate)

### Test: `python stress_test.py --clients 20 --broadcasts 200 --delay 0.01` (200 msg/s)

```
Results:
  Clients connected:         ___
  Delivery rate:             ___ %
  Min/Avg/P99/Max latency:   ___ / ___ / ___ / ___ ms
  Total test time:           ___ s
  Retransmissions triggered: ___ (estimated)
  
Observations:
  [Did the system handle fast bursts? Any queuing delays?]
```

---

## 5. Reliability Test (Packet Loss Simulation)

### Test A: 5% Simulated Loss
```
python performance_test.py --packets 100 --loss 5 --seed 42

Results:
  Best-Effort delivery rate:  ___ %
  Reliable delivery rate:     ___ %
  Improvement:                +___ pp
```

### Test B: 20% Simulated Loss
```
python performance_test.py --packets 100 --loss 20 --seed 42

Results:
  Best-Effort delivery rate:  ___ %
  Reliable delivery rate:     ___ %
  Improvement:                +___ pp
```

**Observations:**
```
[How well did the ACK+retransmit mechanism handle loss?]
```

---

## 6. Edge Cases & Fault Handling

### Test: Abrupt Client Disconnection
```
Procedure:
  1. Start server + 3 clients
  2. Send a message → all receive OK
  3. Kill one client process (Ctrl+C)
  4. Send another message
  5. Observe: remaining clients receive, dead client removed from list

Results:
  ✓ Server detected client disconnect within ___ seconds
  ✓ Other clients unaffected
  ✓ No server errors/crashes
  
Observations:
  [How long did it take to evict the dead client?]
```

### Test: Invalid JSON Over Control Channel
```
Procedure:
  1. Start server + 1 client
  2. Connect TLS manually, send garbage JSON
  
Results:
  ✓ Server logs warning but doesn't crash
  ✓ Other clients unaffected
  
Observations:
  [Error handling adequate?]
```

### Test: Invalid UDP Packet (Corrupted Ciphertext)
```
Procedure:
  1. Start server + 1 client
  2. Send a broadcast
  3. Intercept & flip bits in ciphertext
  4. Observe: client rejects with auth failure
  
Results:
  ✓ GCM tag verification fails as expected
  ✓ Packet dropped silently
  
Observations:
  [Security boundary working correctly?]
```

### Test: SSL Handshake Failure
```
Procedure:
  1. Start server
  2. Connect client to wrong certificate (wrong server.crt)
  
Results:
  ✗ Connection rejected with SSLError
  ✓ No server crash
  ✓ Other clients unaffected
  
Observations:
  [Certificate verification working?]
```

---

## 7. Security Verification

### Test: Encryption Coverage
- [ ] All UDP packets encrypted (sample-check with Wireshark/tcpdump)?
- [ ] TLS session uses TLS 1.2+?
- [ ] Session keys are unique per client?
- [ ] No plaintext secrets logged?

**Evidence:**
```
[Describe how you verified each]
```

---

## 8. Performance Bottlenecks & Observations

### CPU Profiling (Optional)
```
[If you used cProfile or similar, note hotspots]
```

### Memory Usage
```
Baseline (no clients):     ___ MB
Per-client overhead:       ___ MB
100 clients estimated:     ___ MB
```

### Network Bandwidth
```
Single broadcast message:  ___ bytes (including header + encryption)
ACK per message:           ___ bytes
Heartbeat per 10s:         ___ bytes per client

Estimated for 100 clients + 1 msg/s:
  Data channel:            ___ KB/s
  Control channel:         ___ KB/s
```

---

## 9. Comparison: Custom Reliability vs Best-Effort

| Metric | Best-Effort | Reliable | Trade-off |
|--------|-------------|----------|-----------|
| Delivery rate (15% loss) | [%] | [%] | +[pp] gain |
| Latency (median) | [ms] | [ms] | +[ms] cost |
| Bandwidth overhead | 0% | [%] | [%] extra |
| Implementation complexity | Simple | Complex | [Justified?] |

---

## 10. Scalability Analysis

### Current Limits
```
Max concurrent clients (tested):     ___
Max messages/second sustained:       ___
Max throughput (bytes/sec):          ___

Limiting factor(s):
  [ ] Server CPU
  [ ] Server memory
  [ ] Network bandwidth
  [ ] Single UDP-Recv thread (contention)
  [ ] Other: ___
```

### Recommendations for 1000+ Clients (if needed)
```
1. Use socket multiplexing (epoll/IOCP) instead of per-thread
2. Add load balancing (multiple server instances)
3. Use asynchronous I/O (asyncio / tokio)
4. Profile and optimize hot paths

This system is optimized for ~100–200 concurrent clients.
Beyond that, architectural changes would be needed.
```

---

## 11. Lessons Learned & Observations

### What Worked Well
- [ ] Socket programming model (low-level, no surprises)
- [ ] TLS for secure key exchange
- [ ] AES-256-GCM for message authentication
- [ ] Thread-per-client model for reliability
- [ ] Sequence numbers + ACK for delivery guarantee

### What Could Be Improved
- [ ] [List any observed issues]
- [ ] [Suggestions for future versions]

### Key Takeaways
```
[Write a paragraph or two summarizing the main insights
 you gained about socket programming, reliability, and security]
```

---

## 12. Viva Talking Points

Use this section to prepare for your final presentation:

1. **Why two channels (TCP/UDP)?**
   - TCP/TLS is reliable for key exchange; UDP is fast for broadcasts
   
2. **Why custom reliability instead of DTLS?**
   - Python doesn't support DTLS; custom reliability gives pedagogical value

3. **How does encryption work?**
   - AES-256-GCM: confidential + authenticated + integrity-checked

4. **What happens when a client suddenly disconnects?**
   - Heartbeat timeout after 35 seconds; other clients unaffected

5. **Performance under load?**
   - [Describe your stress test results]

6. **Security guarantees?**
   - [Explain TLS + GCM coverage]

---

## Appendix: Raw Test Output

### Test 1 Output
```
[Paste actual console output from stress_test.py here]
```

### Test 2 Output
```
[Paste actual console output from performance_test.py here]
```

### Test 3 Output
```
[Paste any other test output]
```

---

**Report compiled by:** [YOUR NAME]  
**Date:** [DATE]  
**Signature:** _________________
