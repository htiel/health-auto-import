# Known Issues

## #1 — HAE TCP server freezes when iOS app is backgrounded

**Status:** Open — upstream iOS limitation, not fixable from the integration side  
**Severity:** Critical — makes the integration unreliable without manual intervention  
**Root cause:** iOS suspends the Health Auto Export app process after ~2–3 minutes in the background. The TCP server becomes unresponsive mid-request. This is standard iOS behavior for apps that have not requested background execution entitlements.

### Evidence

Five diagnostic dumps analyzed (v0.0.9 through v0.0.13), all showing the same pattern:

| Dump | Version | Uptime before freeze | Freeze on |
|------|---------|---------------------|-----------|
| 2026-05-24T03-33-30Z | v0.0.9 | ~3 min | (no freeze — app stayed foreground) |
| 2026-05-24T03-37-21Z | v0.0.10 | ~3 min | health_metrics (heavy) |
| 2026-05-24T03-46-36Z | v0.0.11 | ~3 min | idle period |
| 2026-05-24T04-04-27Z | v0.0.12 | ~3 min | workouts (20ms call — light) |
| 2026-05-24T04-11-56Z | v0.0.13 | 2 min 21s | unknown tool (request decoded, never completed) |

Key finding: the freeze happens on **light calls** (workouts, heart_notifications) just as often as heavy ones (health_metrics), proving it is not caused by query complexity or our request volume.

### What we tried

| Version | Change | Result |
|---------|--------|--------|
| v0.0.8 | `asyncio.Lock` — serialize all TCP connections | Prevented concurrent connections overwhelming the server. Did not prevent freeze. |
| v0.0.9 | `ConnectionResetError` handling — catch `OSError` on read/drain | Graceful recovery when server drops mid-response. Did not prevent freeze. |
| v0.0.11 | Inter-request cooldown (1.0s between calls) | Reduced burst pressure on server. Did not prevent freeze. |
| v0.0.12 | Reduced discovery from 10 probe calls to 1 `listTools` + 1 `health_metrics`. Skip standalone probe at startup. Cooldown reduced to 0.5s. | Setup time dropped from ~30s to ~8s. Did not prevent freeze. |
| v0.0.13 | Per-tool read timeouts: 15s default, 30s for `health_metrics` | Faster recovery (lock held 15s instead of 30s during freeze). Did not prevent freeze. |

### What would fix it

The HAE iOS app developer would need to implement one of:
- `BGProcessingTaskRequest` for periodic background execution
- A Network Extension entitlement to keep the TCP server alive
- `beginBackgroundTask(withName:)` for short-lived keep-alive pings

### Workaround

Keep the HAE app in the **foreground** on the iOS device:
- Use **Guided Access** or **Single App Mode**
- Set **Auto-Lock = Never**
- Keep the device on a charger

---

## #2 — Sensors show "Unavailable" when the HAE server is frozen

**Status:** Open — expected behavior given issue #1  
**Severity:** Medium — sensors recover automatically when the app is reopened  
**Root cause:** When the TCP server is frozen (issue #1), coordinator `_async_update_data()` calls time out. Home Assistant marks the entities as Unavailable.

### Behavior

- All health sensors (ECG, workouts, heart rate, medications, health metrics) go Unavailable simultaneously
- The reachability binary sensor flips to OFF
- On the next successful poll after reopening the HAE app, all sensors recover with fresh data
- No data is lost — the integration uses watermarks to resume from the last successful fetch

### Recovery

Force-close and reopen the Health Auto Export app on the iOS device. Sensors recover within one poll interval (60s for workouts/heart, 10 min for health metrics).

---

## #3 — "receive failed" errors in HAE diagnostic logs (benign)

**Status:** Closed — expected behavior, no action needed  
**Severity:** Low — cosmetic log noise only  
**Root cause:** The integration uses connection-per-request: connect → send → read → close. When we close the socket after reading the response, the HAE server's read loop sees `NWError 89 — Operation canceled` and logs it as an error.

### Example

```
{"msg":"receive failed","lvl":"error","meta":{"detail":"The operation couldn't be completed. (Network.NWError error 89 - Operation canceled)"}}
```

This appears after **every** successful call and can be safely ignored. The response was already read and processed before the socket was closed.

---

## #4 — Unit mapping mismatches causing HA recorder warnings

**Status:** Fixed in v0.0.10 / v0.0.12  
**Severity:** Medium — caused `WS_TYPE_STATISTICS_ISSUES` warnings and orphaned statistics  

### Problem

HA's recorder expects `unit_of_measurement` to remain stable for long-term statistics. Our `_UNIT_MAP` was translating HAE units (e.g., `count/min` → `bpm`, `dBASPL` → `dBA`, `count` → `""`) which created mismatches when the unit changed between polls or after restarts.

### Fix

Stripped `_UNIT_MAP` to only two mappings: `degF` → `°F` and `degC` → `°C`. All other HAE units are passed through as-is. Deleted 76 orphaned statistics entries manually via Developer Tools → Statistics.

---

## #5 — Slow initial setup triggering HA "Waiting for integrations" warning

**Status:** Fixed in v0.0.12  
**Severity:** Medium — HA logs a warning if setup takes >10s  

### Problem

Discovery was making 10 separate TCP calls (1 `listTools` + 9 per-tool probes) with 1.0s cooldown between each. Total setup time: ~30 seconds. HA logged:
```
Waiting for integrations to complete setup: health_auto_import
```

### Fix

Reduced discovery to 2 calls: `listTools` + one `health_metrics` call (for metric enumeration). Skipped standalone probe at startup (discovery proves reachability). Reduced cooldown to 0.5s. Setup now completes in ~8 seconds.
