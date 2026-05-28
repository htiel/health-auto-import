# 06 — On-Demand HAE Query Architecture (LCARS Integration)

> **Audience.** LCARS dashboard team building Sickbay history views (multi-ECG list, workout calendar, sleep timeline, BP trend, etc.).
> **Integration version required.** `health_auto_import` ≥ **v1.5.0**.
> **TL;DR.** Stop trying to render history from sensor attributes. Call the new `health_auto_import.query_*` services with `return_response: true`. Apple Health stays the source of truth; HAI is a thin pass-through to the HAE TCP server; LCARS owns the cache and rendering.

---

## 1. Architecture

```
┌──────────────┐    callWS('call_service',          ┌────────────────────┐
│ LCARS card   │ ─── return_response: true) ──────▶ │ HAI (this plugin)  │
│ (lit-html)   │                                    │  services.py       │
└──────────────┘ ◀── ServiceResponse JSON ───────── └─────────┬──────────┘
       ▲                                                      │ HaeClient.call_tool
       │                                                      ▼
       │                                            ┌────────────────────┐
       │                                            │ HAE TCP server     │
       │                                            │ (iPad, port 9000)  │
       │                                            └─────────┬──────────┘
       │                                                      │
       │                                                      ▼
       └─── live sensor.hae_* (latest only) ◀── Apple Health (truth)
```

**Key invariants**

- **Apple Health is canonical.** HAI never persists query results; nothing is written to disk as a side effect of a service call.
- **Latest-record sensors are untouched.** `sensor.hae_last_ecg`, `sensor.hae_last_workout`, etc. still work the same way (independent poll loop, persisted across restarts). Use them for the "current value" tiles. Use the new services for "show me the last 90 days".
- **Watermarks are not advanced by service calls.** A history pull doesn't poison the next live poll.
- **No 40 KiB attribute cap.** Service responses go over the WebSocket as a `ServiceResponse`; they are not stored as entity attributes.

---

## 2. Services available

All services live in the `health_auto_import` domain and are registered with `SupportsResponse.ONLY` — every call **must** include `return_response: true`.

| Service | Use for |
|---|---|
| `query_ecg` | ECG records (incl. `voltage_uv` waveform, downsample yourself if needed) |
| `query_workouts` | Workouts (HR series + GPS route are opt-in flags, default true) |
| `query_metrics` | Health metric buckets (steps, HR, sleep, BP, weight, …) |
| `query_medications` | Medication log entries |
| `query_heart_notifications` | High/low/irregular HR notifications |
| `query` | Generic — any tool, any arguments (escape hatch) |

### Common fields

| Field | Type | Default | Notes |
|---|---|---|---|
| `start` | ISO datetime | `end - days` | UTC assumed if no offset |
| `end` | ISO datetime | now | UTC assumed if no offset |
| `days` | int 1..2555 | 30 | Shortcut when `start` omitted |
| `limit` | int 1..500 | unlimited | Trims to **most recent N** records server-side-of-response |

### Service-specific fields

| Service | Field | Default | Notes |
|---|---|---|---|
| `query_workouts` | `include_routes` | `true` | Set `false` to skip GPS payload for fast list views |
| `query_workouts` | `include_metadata` | `true` | Set `false` to skip HR series |
| `query_metrics` | `metrics` | all | List or CSV of metric IDs, e.g. `["step_count","heart_rate"]` |
| `query` | `tool` | — | Required. HAE tool name |
| `query` | `arguments` | `{}` | Merged into the JSON-RPC call after `start`/`end` |

### Response envelope

Every service returns the same shape:

```jsonc
{
  "tool": "workouts",
  "count": 12,
  "arguments": { "start": "2026-02-26 00:00:00 +0000", "end": "...", "includeRoutes": true, "includeMetadata": true },
  "data": {
    "workouts": [ /* records, oldest → newest */ ]
  }
}
```

`data` is the raw HAE payload — same shape the live sensors parse from. The record list key depends on the tool: `data.ecg`, `data.workouts`, `data.metrics`, `data.medications`, `data.heartRateNotifications`. The generic envelope normalises `count` for you.

---

## 3. Recommended LCARS pattern

### 3.1. WebSocket helper

Put this in `lcars-dashboard/src/api/health-history.ts` (or your existing API layer):

```ts
type QueryArgs = {
  days?: number;
  start?: string;        // ISO
  end?: string;          // ISO
  limit?: number;
  [extra: string]: unknown;
};

type QueryResult<T> = {
  tool: string;
  count: number;
  arguments: Record<string, unknown>;
  data: { [tool: string]: T[] } & Record<string, unknown>;
};

export async function queryHealth<T>(
  hass: HomeAssistant,
  service: 'query_ecg' | 'query_workouts' | 'query_metrics'
         | 'query_medications' | 'query_heart_notifications',
  args: QueryArgs,
): Promise<QueryResult<T>> {
  const resp = await hass.callWS<{ response: QueryResult<T> }>({
    type: 'call_service',
    domain: 'health_auto_import',
    service,
    service_data: args,
    return_response: true,
  });
  return resp.response;
}
```

### 3.2. Component pattern — render-on-demand, cache in memory

```ts
class LcarsEcgHistoryTile extends LitElement {
  @state() private records: EcgRecord[] = [];
  @state() private loading = false;
  @state() private error: string | null = null;

  private async load() {
    this.loading = true;
    this.error = null;
    try {
      const r = await queryHealth<EcgRecord>(this.hass, 'query_ecg', {
        days: 90,
        limit: 10,
      });
      this.records = (r.data.ecg ?? []) as EcgRecord[];
    } catch (e: any) {
      this.error = String(e?.message ?? e);
    } finally {
      this.loading = false;
    }
  }

  protected firstUpdated() { this.load(); }
}
```

### 3.3. When to call (and when not to)

| Situation | What to do |
|---|---|
| Card mounts (user opens Sickbay) | Fire the relevant `query_*` once |
| User changes date range / filter | Re-query with new args |
| Live "latest" tile | **Don't** query — read `sensor.hae_last_*` |
| Repeated mount of the same card | Cache in the component for the session; do not query on every render |
| Background refresh while card is mounted | Optional — set `setInterval(..., 15 * 60 * 1000)` for a 15-min refresh |
| HAE server offline | Service call throws `HomeAssistantError` — render last-known sensor value + a "history unavailable" badge |

---

## 4. Query recipes

### Last 5 ECGs (for the ECG carousel)

```ts
await queryHealth(hass, 'query_ecg', { days: 90, limit: 5 });
```

### Workouts in a specific month (calendar view)

```ts
await queryHealth(hass, 'query_workouts', {
  start: '2026-03-01T00:00:00Z',
  end:   '2026-03-31T23:59:59Z',
  include_routes: false,    // skip 100s of KB of GPS for a list view
  include_metadata: false,  // skip HR series too
  limit: 100,
});
```

Then fetch a single workout's full payload only when the user opens it:

```ts
await queryHealth(hass, 'query_workouts', {
  start: pickedWorkout.start,
  end:   pickedWorkout.end,
  limit: 1,
  // include_routes/include_metadata default to true
});
```

### Last 7 days of sleep

```ts
await queryHealth(hass, 'query_metrics', {
  days: 7,
  metrics: ['sleep_analysis'],
});
```

### Blood pressure trend (last 90 days)

```ts
await queryHealth(hass, 'query_metrics', {
  days: 90,
  metrics: ['blood_pressure_systolic', 'blood_pressure_diastolic'],
});
```

### Generic escape hatch

```ts
await queryHealth(hass, 'query' as any, {
  tool: 'some_new_tool',
  days: 14,
  arguments: { customFlag: true },
});
```

---

## 5. Sizing & performance

| Tool | Typical record size | Notes |
|---|---|---|
| ECG | ~2–5 MB each with full `voltage_uv` (15 360 floats) | Downsample client-side for waveform display. The live sensor already downsamples to 2 000 points |
| Workouts (no routes/metadata) | ~1–3 KB each | Cheap; safe to pull 100+ |
| Workouts (full) | tens of KB to MB each | Pull one at a time for detail views |
| Metrics | varies per metric, usually < 50 KB per bucket | `metrics` filter is your friend |
| Medications | ~300 B each | Cheap |
| HRN | ~500 B each | Cheap |

**Hard limits**

- `MAX_RESPONSE_BYTES` in HAI = **4 MiB** per JSON-RPC response. A request that would exceed this fails. → Don't ask for all 410 ECGs with voltage at once; page by date.
- HAE server is single-threaded. Concurrent requests are serialised by HAI's `_lock`. Fire one query per card mount; don't fan out.
- HAE has a ~2–3 min freeze bug after sustained polling. If a query fails, surface it and let the user retry; don't auto-retry in a tight loop.

---

## 6. Error handling contract

| Failure | Symptom on LCARS side | Recommended UX |
|---|---|---|
| HAE server offline | `callWS` rejects with `HomeAssistantError: HAE query for tool '…' failed: connect …` | Show last-known sensor + "Live history unavailable — using cached current reading" |
| Query window invalid (`start ≥ end`) | `HomeAssistantError: start (…) must be before end (…)` | Validate before calling |
| Tool not supported | `HaeProtocolError [-32602]` wrapped in `HomeAssistantError` | Hide the card; log to console once |
| Response exceeds 4 MiB | `HaeTransportError: response exceeds MAX_RESPONSE_BYTES` | Narrow the date range or set `include_routes: false` |
| Integration not configured | `HomeAssistantError: health_auto_import is not configured` | Suppress the card entirely |

Never silently swallow errors — surface a one-line status in the card chrome so the user knows the difference between "no data" and "couldn't reach Apple Health".

---

## 7. Do / Don't

**Do**

- Use `query_*` for any view showing more than the single latest record.
- Read `sensor.hae_last_*` for live "now" tiles.
- Cache responses in the component for the session.
- Pass `include_routes: false` / `include_metadata: false` on list views.
- Use the `metrics` filter on `query_metrics` to keep payloads small.

**Don't**

- Don't poll the services on a timer faster than 5 min (you'll trip the HAE freeze).
- Don't try to mirror the data into HA recorder via `query_*` — that's what the live sensors are for.
- Don't read `voltage_uv` from `sensor.hae_last_ecg` attributes for multi-ECG views — call `query_ecg` instead; sensor attrs are downsampled and capped at the 40 KiB attribute limit.
- Don't fan out parallel `query_*` calls on card mount; serialise them or you'll hit the HAE single-thread lock.

---

## 8. Reference

- Service definitions: [`custom_components/health_auto_import/services.py`](../../custom_components/health_auto_import/services.py)
- Service schemas: [`custom_components/health_auto_import/services.yaml`](../../custom_components/health_auto_import/services.yaml)
- HAE TCP protocol: see the integration's `HealthHandoff/02-hae-tcp-protocol.md` (local-only reference, not shipped with the repo).
- Latest-record sensor contract: see `HealthHandoff/03-integration-spec.md` (local-only).
