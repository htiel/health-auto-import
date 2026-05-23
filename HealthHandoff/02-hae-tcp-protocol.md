# 02 — HAE TCP Server Protocol Reference

> Everything in this document was reverse-engineered against **Health Auto Export v9.0.9, build 20260519.2**, running on `iPad13,4` (iPadOS 26.5) at `192.168.1.203:9000`. The protocol identifies itself as version `v0.0.1` (legacy).

## Wire format

- **Transport:** raw TCP, no TLS. Default port `9000`.
- **Encoding:** UTF-8.
- **Framing:** one JSON-RPC request per TCP connection. Send the request followed by a newline (`\n`). The server replies with one JSON-RPC response. Close the connection after parsing the response. **Do not attempt to pipeline multiple requests on one socket — the server will hang.**
- **Read strategy that works:** loop on `socket.recv()` accumulating into a buffer; after each chunk attempt `json.loads(buffer.strip())`. As soon as it parses, you have the full response — close the socket. Apply an overall read timeout (15–30 s is plenty).

## Request envelope (LEGACY v0.0.1)

```json
{
  "jsonrpc": "2.0",
  "id": "<random string or int>",
  "method": "callTool",
  "params": {
    "name": "<tool_name>",
    "arguments": { ... }
  }
}
```

> **Important.** Do **not** use the newer MCP-standard `"method": "tools/call"`. The legacy server uses `"method": "callTool"`. If you send `tools/call` it returns a "method not found"-style error.

## Response envelope

The server returns a JSON-RPC response. There are two flavours of `result` you'll see in the wild — your parser must handle both:

### Direct shape (this is what HAE v9.0.9 currently returns)

```json
{
  "jsonrpc": "2.0",
  "id": "...",
  "result": {
    "data": {
      "ecg": [ ... ],
      "workouts": [ ... ],
      "metrics": [ ... ],
      ...
    }
  }
}
```

### MCP `content[0].text` shape (older builds may use this)

```json
{
  "jsonrpc": "2.0",
  "id": "...",
  "result": {
    "content": [
      { "type": "text", "text": "<stringified JSON with the same `data` shape>" }
    ]
  }
}
```

Recommended unwrap logic:

```python
def extract_payload(rpc_response: dict) -> dict:
    result = rpc_response.get("result") or {}
    if "data" in result:
        return result["data"]
    content = result.get("content") or []
    if content and isinstance(content[0], dict) and "text" in content[0]:
        return json.loads(content[0]["text"]).get("data", {})
    return {}
```

## Date format used in arguments

```text
yyyy-MM-dd HH:mm:ss ±HHMM
```

Note the **space** before the offset and the **no-colon** offset. Examples:

```text
2026-04-22 13:21:55 -0700
2026-05-22 13:21:55 -0700
```

Python helper:

```python
def hae_ts(dt: datetime) -> str:
    # dt must be timezone-aware
    return dt.strftime("%Y-%m-%d %H:%M:%S %z")
```

## Tools (validated)

| Tool name | Required args | Optional args | Result shape | Notes |
|---|---|---|---|---|
| `ecg` | `start`, `end` | — | `{ data: { ecg: [ { voltageMeasurements: [{date,units,voltage}], ... } ] } }` | Can return **megabytes** if `voltageMeasurements` are included for many ECGs. Each ECG has classification, average BPM, sampling frequency, etc. (see sample). |
| `workouts` | `start`, `end` | `includeMetadata: bool`, `includeRoutes: bool` | `{ data: { workouts: [ { id, name, start, end, activeEnergyBurned: {qty,units}, ... } ] } }` | Setting both `includeMetadata` and `includeRoutes` to `false` keeps responses small (< 10 KB for ~5 workouts). Routes can be tens of MB. |
| `heart_notifications` | `start`, `end` | — | `{ data: { heartRateNotifications: [ ... ] } }` or `{ data: {} }` if no events | The "no data" empty-shape response is normal. Don't treat as an error. |
| `health_metrics` | `start`, `end`, `metrics` (CSV string) | `interval`: `"days" \| "hours" \| "minutes"`, `aggregate: bool` | `{ data: { metrics: [ { name, units, data: [{date, qty, source}] } ] } }` | Pass metrics as a comma-separated string of HealthKit-style identifiers (see metric ID list below). |
| `medications` | `start`, `end` | — | *(to be validated — HAE supports medication logging; the server tool name is presumed to be `medications`. If it returns a "method not found"-style error, try `medication_logs` or check the server's `tools/list` discovery method.)* | The integration should attempt this; if unavailable, mark the medication entities as `unavailable` rather than failing. |
| `state_of_mind` | `start`, `end` | — | *(also to be validated)* | Skip unless the user opts in. |
| `symptoms` | `start`, `end` | — | *(also to be validated)* | Skip unless the user opts in. |

> **Discovery.** Try sending `{"jsonrpc":"2.0","id":"1","method":"tools/list"}` and `{"jsonrpc":"2.0","id":"1","method":"listTools"}` on first connect to enumerate available tools. If neither works on v0.0.1, fall back to the static map above.

## Metric IDs (snake_case)

These are the IDs that go in the `metrics` argument to `health_metrics`. They mirror HAE's `metrics: [...]` automation list converted to `snake_case`. **Confirmed working from the probe:** `step_count`, `active_energy`. The full set HAE exposes (from `automations.json`):

```text
active_energy, alcohol_consumption, apple_exercise_time, apple_move_time,
apple_sleeping_wrist_temperature, apple_stand_hour, apple_stand_time,
atrial_fibrillation_burden, basal_body_temperature, biotin,
blood_alcohol_content, blood_glucose, blood_oxygen_saturation, blood_pressure,
body_fat_percentage, body_mass_index, body_temperature, breathing_disturbances,
caffeine, calcium, carbohydrates, cardio_recovery, chloride, cholesterol,
chromium, copper, cycling_cadence, cycling_distance,
cycling_functional_threshold_power, cycling_power, cycling_speed,
dietary_energy, distance_downhill_snow_sports, electrodermal_activity,
environmental_audio_exposure, fiber, flights_climbed, folate,
forced_expiratory_volume_1, forced_vital_capacity, handwashing,
headphone_audio_exposure, heart_rate, heart_rate_variability, height,
inhaler_usage, insulin_delivery, iodine, iron, lean_body_mass, magnesium,
manganese, mindful_minutes, molybdenum, monounsaturated_fat, niacin,
number_of_times_fallen, pantothenic_acid, peak_expiratory_flow_rate,
peripheral_perfusion_index, phosphorus, physical_effort, polyunsaturated_fat,
potassium, protein, push_count, respiratory_rate, basal_energy_burned,
resting_heart_rate, riboflavin, running_ground_contact_time, running_power,
running_speed, running_stride_length, running_vertical_oscillation,
saturated_fat, selenium, sexual_activity, six_minute_walking_test_distance,
sleep_analysis, sodium, stair_speed_down, stair_speed_up, step_count,
dietary_sugar, swimming_distance, swimming_stroke_count, thiamin,
time_in_daylight, toothbrushing, total_fat, uv_exposure, underwater_depth,
underwater_temperature, vo2_max, vitamin_a, vitamin_b12, vitamin_b6,
vitamin_c, vitamin_d, vitamin_e, vitamin_k, waist_circumference,
walking_running_distance, walking_asymmetry_percentage,
walking_double_support_percentage, walking_heart_rate_average, walking_speed,
walking_step_length, dietary_water, weight_body_mass, wheelchair_distance, zinc
```

## Errors

JSON-RPC error responses follow standard shape:

```json
{ "jsonrpc": "2.0", "id": "...", "error": { "code": -32601, "message": "..." } }
```

Treat any non-2xx-equivalent response (i.e., `error` key present) as an error and surface it. Don't crash the coordinator.

## Reachability handling

- TCP connect fails → iPad asleep / HAE not foregrounded / wrong network. **Mark integration `unavailable`, flip the reachability binary_sensor OFF.**
- TCP connect succeeds but read times out → HAE wedged. **Flip reachability OFF, log warning.**
- Got a valid JSON-RPC response (even with `error`) → reachability ON. Surface the error on the specific data type only.

## Gotchas observed during reverse-engineering

1. The server may take 1–3 seconds before sending any response bytes. Use a *read timeout*, not an immediate non-blocking read.
2. The server does not send `Content-Length`; you must parse incrementally and stop when you have a complete top-level JSON object.
3. When `voltageMeasurements` are included, individual ECG records can be hundreds of KB each. Strongly recommend default = strip voltage samples from the entity state and either (a) discard them, (b) put them on a separate entity guarded by a config option, or (c) write them to a file. Do not let multi-MB payloads land in HA's recorder.
4. HAE's clock uses the iPad's locale-formatted timestamps inside payloads. Don't assume ISO-8601 in nested fields — parse with a tolerant parser.
