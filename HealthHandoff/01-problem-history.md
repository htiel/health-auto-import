# 01 — Problem History: Why MQTT Was Abandoned

## Original setup

The user configured **4 Health Auto Export (HAE) automations** on an iPad, each exporting a different data type via MQTT to the Mosquitto broker addon running on Home Assistant:

| Automation | Data | Topic | Cadence | Status |
|---|---|---|---|---|
| Metrics  | `healthMetrics` (steps, HR, weight, sleep, etc.) | `leith/health/metrics` | 30 min | ✅ Works |
| Workouts | `workouts`                                       | `leith/health/workouts` | 60 min | ❌ Fails |
| ECG      | `ecg`                                            | `leith/health/ecg`      | 60 min | ❌ Fails |
| HRN      | `heartRateNotifications`                         | `leith/health/heart_rate_notifications` | 60 min | ❌ Fails |

## Diagnosis timeline

### Step 1 — Ruled out HealthKit permissions

Initial hypothesis was missing iOS HealthKit permissions on the iPad. **Disproven** by directly querying HAE's built-in TCP server (`192.168.1.203:9000`) using `samples/query.ps1`:

- `ecg` returned **2,191,062 bytes** of real voltage measurements.
- `workouts` returned **7,887 bytes** of real workout data ("Outdoor Walk", 2026-05-21).
- `heart_notifications` returned `{"data":{}}` (empty — no events in 30 days, but no error).
- `health_metrics` returned step counts, energy, etc.

So **HAE has the data**. The failure is in HAE's MQTT publish pipeline only.

### Step 2 — Ruled out shared clientID

All 4 automations originally had identical `clientID: "AppleHealth-Leith"`. MQTT 3.1.1 spec requires unique client IDs; brokers kick the older session when a duplicate connects. User fixed this by assigning unique IDs: `-met`, `-wo`, `-ecg`, `-hrn`. **The fix did not resolve the failures.**

### Step 3 — Ruled out broker-side issues using live `$SYS` monitoring

Set up a Python paho-mqtt subscriber on `leith/health/#` plus `$SYS/broker/log/#`, `$SYS/broker/clients/#`, `$SYS/broker/messages/#` to capture every broker event. Then user manually triggered Workouts and ECG.

**Broker logs showed:**

```text
14:00:27 New connection from 192.168.1.203:49497
14:00:27 New client connected from 192.168.1.203:49497 as AppleHealth-Leith-wo (p4, c1, k60, u'hau-mqtt')
14:01:32 Client AppleHealth-Leith-wo disconnected: connection closed by client
```

- CONNECT succeeds.
- Authentication succeeds (`u'hau-mqtt'`).
- The client **stays connected for ~65 seconds and never sends a single PUBLISH packet**.
- Then HAE proactively closes it.

Meanwhile `AppleHealth-Leith-met` connects, publishes 208 KB, and disconnects cleanly in < 1 second. Same broker, same network path, same auth, same iPad.

### Step 4 — HAE event log confirms in-app failure

HAE's `events.jsonl` export shows the failing automations record:

```text
mqtt_upload_foreground   info
mqtt_upload_outcome      error  errorType: MQTTPublishError    (~13 ms later)
mqtt_upload_outcome      error  errorType: RuntimeError
```

**~13 ms between attempt and failure is impossible for a real network round-trip.** The exception is raised *inside the HAE app*, between "got CONNACK" and "send PUBLISH". The broker never sees a publish for the failing data types.

### Conclusion

The failure is a bug inside HAE's MQTT publish code path for ECG and Workouts specifically. Likely a payload-serialization exception that is caught and re-raised as `MQTTPublishError`. The bug cannot be fixed from outside the app.

For HRN, the bug is different: when HAE's data query returns an empty document (`{"data":{}}`), the MQTT publish step throws a `RuntimeError` (the "no data" error the user sees in the UI). Also unfixable from outside.

## Why pulling from the TCP server fixes all of this

- The TCP/MCP server is HAE's own debug/automation API. Querying it bypasses the broken MQTT publish code entirely.
- It returns clean JSON. We've already validated this.
- Polling cadence, retries, throttling, error logging, and empty-result handling all live in **our** code, where we control them.
- The user gets a single reachability `binary_sensor` to notify them when the iPad goes offline, instead of silent MQTT failures.

## Constraints carried forward

1. **HAE foreground requirement.** The TCP server only runs while HAE is foregrounded on the iPad. iOS will background-suspend the app after several minutes if the iPad sleeps. Mitigations: Guided Access, Single App Mode, dock with always-on, "Don't lock" Auto-Lock setting. The integration should not assume 100% uptime — it must handle connection failures gracefully and surface them via the reachability sensor.
2. **One request per TCP connection.** The server expects a single JSON-RPC request, returns a single response, then the client should close. Don't pipeline. Our probe (`samples/query.ps1`) demonstrates a working open → write → read-until-parseable → close flow.
3. **15-second response window has been sufficient.** Even the 2 MB ECG payload returns within a few seconds. A 30 s read timeout per request is a safe default.
4. **Server version v0.0.1 (legacy).** Uses `method: "callTool"` with `params: { name, arguments }`. Not the newer MCP `tools/call` envelope. See `02-hae-tcp-protocol.md`.
