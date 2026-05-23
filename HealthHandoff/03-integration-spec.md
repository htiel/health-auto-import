# 03 — Integration Specification

## Identity

| Field | Value |
|---|---|
| Suggested domain | `health_auto_export` |
| Display name | `Health Auto Export` |
| IoT class | `local_polling` |
| Config flow | `True` |
| Iot protocol | TCP / JSON-RPC v0.0.1 (HAE proprietary) |
| Requires HACS | Yes (standard custom_components install also works) |
| Python deps | None beyond stdlib — `asyncio`, `json`, `socket`. Optionally `zeroconf` (already an HA dep). |

## Repository layout (target)

```
health-auto-export-ha/
├── README.md
├── hacs.json
├── info.md
├── LICENSE
└── custom_components/
    └── health_auto_export/
        ├── __init__.py
        ├── api.py                 # TCP client + JSON-RPC envelope + tool wrappers
        ├── binary_sensor.py       # reachability + has_active_workout
        ├── config_flow.py         # manual host/port + zeroconf discovery
        ├── const.py
        ├── coordinator.py         # one DataUpdateCoordinator per data type
        ├── entity.py              # shared base entity
        ├── manifest.json
        ├── sensor.py              # all sensor entities
        ├── strings.json
        └── translations/
            └── en.json
```

## Config flow

### Manual setup
- Field: **Host** (string, required, default suggestion from zeroconf if available)
- Field: **Port** (int, required, default `9000`)
- Validation: open TCP socket; if it succeeds attempt `tools/list` then fall back to a known tool with a tiny date window (e.g. `health_metrics` with `metrics=heart_rate` over last 1 hour). If a valid JSON-RPC response is returned, accept the entry.

### Zeroconf autodiscovery (optional but nice)
- HAE advertises `_health-export._tcp.local.` Bonjour service from some versions — **TO VERIFY** during implementation. Suggest scanning at `_http._tcp.local.` for hostnames starting with the iPad name and probing `/`. If unsupported, leave the discovery code path stubbed and ship manual-only for v1.

### Options flow (post-setup reconfiguration)
- **Polling intervals (minutes)**:
  - Health metrics: default **5**
  - ECG: default **5**
  - Workouts: default **1**
  - Heart-rate notifications: default **1**
  - Medications: default **15**
- **History window (days)** per data type (default ECG = 30, Workouts = 7, HRN = 7).
- **Selected metrics** — multi-select against the snake_case ID list in `02-hae-tcp-protocol.md`. Default to the **Sickbay** subset:
  - `heart_rate`, `resting_heart_rate`, `walking_heart_rate_average`
  - `heart_rate_variability`
  - `blood_oxygen_saturation`, `blood_pressure`, `body_temperature`, `respiratory_rate`
  - `blood_glucose`
  - `step_count`, `active_energy`, `apple_exercise_time`, `apple_stand_hour`, `flights_climbed`
  - `weight_body_mass`, `body_fat_percentage`, `body_mass_index`, `lean_body_mass`, `height`
  - `vo2_max`, `cardio_recovery`
  - `sleep_analysis`, `apple_sleeping_wrist_temperature`, `mindful_minutes`
  - `atrial_fibrillation_burden`, `number_of_times_fallen`
- **Include raw ECG voltage in attributes** — bool, default **False** (see warning in doc 02).

## Coordinators

One `DataUpdateCoordinator` per data type, all sharing the same `api.HaeClient` instance:

| Coordinator | Tool | Default interval | Notes |
|---|---|---|---|
| `MetricsCoordinator` | `health_metrics` | 5 min | Pulls the user-selected metric IDs in one call. |
| `EcgCoordinator` | `ecg` | 5 min | Stores the latest ECG summary; voltage array gated by option. |
| `WorkoutsCoordinator` | `workouts` | 1 min | Detects in-progress workouts (`end` timestamp missing or in the future). |
| `HrnCoordinator` | `heart_notifications` | 1 min | `{"data":{}}` → coordinator data = `{notifications: []}`. |
| `MedicationsCoordinator` | `medications` | 15 min | If the tool returns "method not found", mark all medication entities `unavailable` and stop scheduling this coordinator. |
| `ReachabilityCoordinator` | n/a (pure TCP probe) | 30 s | Lightweight `tcp_connect` probe. Drives the `binary_sensor.health_auto_export_reachable`. |

The reachability coordinator's state is also reflected by each data coordinator's `last_update_success`; consumers can subscribe to either.

## Entities (Sickbay-focused)

### `binary_sensor`
| Entity ID | Device class | Source | Notes |
|---|---|---|---|
| `binary_sensor.hae_reachable` | `connectivity` | reachability coordinator | ON when last TCP probe succeeded. **Use this to drive a HA notification when the iPad goes offline.** |
| `binary_sensor.hae_workout_in_progress` | `running` | workouts coordinator | ON when any workout has `start` < now and (`end` is null OR `end` > now). |
| `binary_sensor.hae_afib_detected_recent` | `safety` | metrics or HRN coordinator | ON if AFib burden > 0 in last 24 h. |

### `sensor` (ECG)
- `sensor.hae_ecg_last_classification` (state: `sinusRhythm` / `atrialFibrillation` / `inconclusive` / `unclassifiable`)
- `sensor.hae_ecg_last_average_bpm` (`unit: bpm`, device_class: `frequency` is wrong — leave `device_class` unset, set `state_class: measurement`)
- `sensor.hae_ecg_last_taken_at` (device_class: `timestamp`)
- `sensor.hae_ecg_count_30d` (`unit: ecgs`, `state_class: total`)

### `sensor` (Workouts)
- `sensor.hae_last_workout_type` (state: e.g. `Outdoor Walk`)
- `sensor.hae_last_workout_duration_min` (`unit: min`, `device_class: duration`, `state_class: measurement`)
- `sensor.hae_last_workout_distance_km` (`unit: km`, `device_class: distance`, `state_class: measurement`)
- `sensor.hae_last_workout_active_energy_kcal` (`unit: kcal`, `device_class: energy`, `state_class: measurement`)
- `sensor.hae_last_workout_avg_hr_bpm`
- `sensor.hae_last_workout_started_at` (`device_class: timestamp`)
- `sensor.hae_workout_count_7d`

### `sensor` (Heart-rate notifications)
- `sensor.hae_hrn_last_type` (state: `high` / `low` / `irregular`)
- `sensor.hae_hrn_last_at` (`device_class: timestamp`)
- `sensor.hae_hrn_count_high_7d`
- `sensor.hae_hrn_count_low_7d`
- `sensor.hae_hrn_count_irregular_7d`

### `sensor` (Vitals — from `health_metrics`)
For every metric the user selects in the options flow, expose:
- `sensor.hae_<metric_id>_latest` (state = most recent `qty`, attributes = `source`, `date`)
- Optionally `sensor.hae_<metric_id>_today_total` for cumulative metrics (`step_count`, `active_energy`, `flights_climbed`, `apple_exercise_time`).
- Use HA's standard `device_class` / `unit_of_measurement` / `state_class` per the unit hints HAE returns (`units` field on each metric data point).

### `sensor` (Medications) — guarded by tool availability
- `sensor.hae_med_<name>_last_taken_at` per active medication.
- `sensor.hae_med_<name>_dose_count_today`.

## Error handling rules

1. Coordinator update raises `UpdateFailed` only on transport errors (TCP connect refused, read timeout, JSON parse failure). All these flip the reachability sensor OFF.
2. JSON-RPC `error` responses → log warning, surface the message in `coordinator.last_exception`, but **do not** flip reachability OFF — the iPad is reachable, just the tool failed.
3. `{"data":{}}` empty responses → log debug only. Coordinator data becomes the empty list/dict for that type. Entities go to `unknown` for "latest"-style sensors and `0` for "count"-style sensors.
4. Per-call timeout: 30 s. Per-coordinator backoff on consecutive failures (HA's built-in coordinator handles this).

## Reachability automation pattern (for the user's docs)

```yaml
alias: "Sickbay: iPad health-export server unreachable"
trigger:
  - platform: state
    entity_id: binary_sensor.hae_reachable
    to: "off"
    for: "00:05:00"
action:
  - service: notify.mobile_app_iphone
    data:
      title: "Sickbay offline"
      message: "Health Auto Export server unreachable. Wake the iPad and re-open HAE."
```

## Versioning and HACS metadata

- `manifest.json` `version`: start at `0.1.0`.
- `hacs.json` at repo root:
  ```json
  {
    "name": "Health Auto Export",
    "render_readme": true,
    "country": ["US"]
  }
  ```
- Use semantic versioning. Tag releases on GitHub for HACS to pick them up.

## Out-of-scope for v1 (defer)

- Pushing data back into HAE (no good use case).
- TLS to the HAE server (HAE doesn't offer it).
- ECG waveform display (huge payloads; do as a separate companion card later).
- Cycle tracking, state of mind, symptoms (sensitive data; opt-in only, defer).

## Testing checklist

- [ ] Config flow validates against a real HAE server at `192.168.1.203:9000`.
- [ ] Config flow rejects a wrong host with a friendly error.
- [ ] Each coordinator's first refresh returns expected data.
- [ ] Reachability binary_sensor transitions OFF within ~30 s of killing HAE on the iPad, and back ON within ~30 s of relaunching.
- [ ] Empty HRN response does not raise; relevant entities show `0` / `unknown`.
- [ ] Reload integration via UI works without restarting HA.
- [ ] HA logs are clean at INFO; useful debug at DEBUG.
