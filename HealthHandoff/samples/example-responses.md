# Example HAE TCP Server Responses

All snippets captured 2026-05-22 from real iPad (`192.168.1.203:9000`, HAE v9.0.9). Values trimmed for readability.

## `ecg` — 30-day window

**Request**
```json
{
  "jsonrpc": "2.0", "id": "1", "method": "callTool",
  "params": {
    "name": "ecg",
    "arguments": { "start": "2026-04-22 13:21:55 -0700", "end": "2026-05-22 13:21:55 -0700" }
  }
}
```

**Response shape** (full payload ≈ 2.19 MB; voltageMeasurements truncated here)
```json
{
  "jsonrpc": "2.0", "id": "1",
  "result": {
    "data": {
      "ecg": [
        {
          "voltageMeasurements": [
            { "date": 1779138148.4899402, "units": "mcV", "voltage": -159.215 },
            { "date": 1779138148.4918933, "units": "mcV", "voltage": -200.955 }
          ]
        }
      ]
    }
  }
}
```

> **Note.** Each ECG record also includes (in fields not shown above due to capture truncation): `classification`, `averageHeartRate`, `samplingFrequency`, `start`, `numberOfMeasurements`, `voltageMeasurementsCount`. The integration should consume those summary fields and gate `voltageMeasurements` behind a config option (default off) so the recorder doesn't ingest megabytes of samples.

## `heart_notifications` — empty (no events in last 30 days)

**Request**
```json
{
  "jsonrpc": "2.0", "id": "2", "method": "callTool",
  "params": {
    "name": "heart_notifications",
    "arguments": { "start": "2026-04-22 13:21:55 -0700", "end": "2026-05-22 13:21:55 -0700" }
  }
}
```

**Response** (55 bytes total)
```json
{ "jsonrpc": "2.0", "id": "2", "result": { "data": {} } }
```

> When notifications exist the shape is `result.data.heartRateNotifications: [ ... ]`. **The integration must not treat the empty object as an error.**

## `workouts` — 30-day window, minimal

**Request**
```json
{
  "jsonrpc": "2.0", "id": "3", "method": "callTool",
  "params": {
    "name": "workouts",
    "arguments": {
      "start": "2026-04-22 13:21:55 -0700",
      "end":   "2026-05-22 13:21:55 -0700",
      "includeMetadata": false,
      "includeRoutes":   false
    }
  }
}
```

**Response** (~ 7.9 KB)
```json
{
  "jsonrpc": "2.0", "id": "3",
  "result": {
    "data": {
      "workouts": [
        {
          "id": "883045E3-F8AE-44CE-BC1E-9C207B5BE467",
          "name": "Outdoor Walk",
          "start": "2026-05-21 07:29:25 -0700",
          "temperature":        { "qty": 10.94,  "units": "degC" },
          "activeEnergyBurned": { "qty": 72.16,  "units": "kcal" },
          "humidity":           { "qty": 83,     "units": "%" },
          "maxSpeed":           { "qty": 9.90,   "units": "km" },
          "speed":              { "qty": 2.92,   "units": "km/hr" },
          "metadata":           { }
        }
      ]
    }
  }
}
```

> Real records also include `end`, `duration`, `distance`, `avgHeartRate`, `maxHeartRate`, `elevationUp/Down`, etc. depending on workout type.

## `health_metrics` — 7-day window, step + energy aggregated daily

**Request**
```json
{
  "jsonrpc": "2.0", "id": "4", "method": "callTool",
  "params": {
    "name": "health_metrics",
    "arguments": {
      "start": "2026-05-15 13:21:55 -0700",
      "end":   "2026-05-22 13:21:55 -0700",
      "metrics":  "step_count,active_energy",
      "interval": "days",
      "aggregate": true
    }
  }
}
```

**Response** (1.27 KB)
```json
{
  "jsonrpc": "2.0", "id": "4",
  "result": {
    "data": {
      "metrics": [
        {
          "name":  "step_count",
          "units": "count",
          "data": [
            { "date": "2026-05-15 13:21:55 -0700", "qty": 3163,    "source": "Ten|Air|Oura" },
            { "date": "2026-05-16 13:21:55 -0700", "qty": 3376,    "source": "Ten|Air|Oura" },
            { "date": "2026-05-17 13:21:55 -0700", "qty": 3773,    "source": "Ten|Air|Oura" },
            { "date": "2026-05-18 13:21:55 -0700", "qty": 6508.1,  "source": "Ten|Air|Oura" },
            { "date": "2026-05-19 13:21:55 -0700", "qty": 10872.9, "source": "Ten|Air|Oura" }
          ]
        }
      ]
    }
  }
}
```

> Pipes in `source` indicate multiple data contributors. Don't split — pass through as-is on the entity attribute.

## Broker $SYS log confirming MQTT silent-publish failure

(Reproduced from `_sub.log`; **not** for the integration but explains why the pull-based design is needed.)

```text
14:00:27 New connection from 192.168.1.203:49497 on port 1883.
14:00:27 New client connected from 192.168.1.203:49497 as AppleHealth-Leith-wo (p4, c1, k60, u'hau-mqtt').
14:01:32 Client AppleHealth-Leith-wo [192.168.1.203:49497] disconnected: connection closed by client.
```

No PUBLISH packet between connect and disconnect for Workouts/ECG. Same flow for Metrics shows an additional `messages/received` increment and immediate clean disconnect with a 208 KB payload, every time. Conclusively: HAE silently drops the payload for those automations before transmission.
