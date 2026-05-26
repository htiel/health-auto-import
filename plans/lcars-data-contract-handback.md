# LCARS Sickbay Data Contract v2 — Handback

**Release:** [v1.1.0](https://github.com/htiel/health-auto-import/releases/tag/v1.1.0)  
**Date:** 2026-05-25  
**Contract:** [health-auto-import-data-contract.md](https://github.com/htiel/LCARS-lovelace-dashboard/blob/5.0/plans/health-auto-import-data-contract.md)  
**Files changed:** `sensor.py`, `coordinator.py`

---

## Implementation status

| Ask | Status | Entity |
|-----|--------|--------|
| §2.1 ECG voltage samples | ✅ Shipped | `sensor.health_auto_import_heart_ecg_voltage_measurements` |
| §2.2 Sleep-stage segments | ✅ Shipped | `sensor.health_auto_import_health_metrics_sleep_analysis_latest` |
| §2.3 Workout HR time series | ✅ Shipped | `sensor.health_auto_import_workouts_workout_last_avg_hr` |
| §2.4 Workout GPS polyline | ✅ Shipped | `sensor.health_auto_import_workouts_workout_last_started` |
| §2.5 Schema probe | ✅ Shipped | All four above |

---

## Exact attribute shapes

### §2.1 — ECG voltage (`ecg_voltage_measurements`)

```yaml
state: 15360                              # numberOfVoltageMeasurements (unchanged)
attributes:
  voltage_uv: [12, -5, 8, 14, ...]       # rounded integers, microvolt
  voltage_unit: "uV"
  classification: "sinusRhythm"
  average_bpm: 78
  duration_s: 30.0
  sampling_frequency_hz: 512
  recorded_at: "2026-05-23T08:17:00-07:00"
  lcars_schema_version: "1"
```

- `voltage_uv` is extracted from HAE's `[{date, units, voltage}, ...]` — only the voltage value, rounded to integer.
- If HAE returns no `voltageMeasurements` key in the ECG record, **all attributes are absent** (not null).
- Entity has `entity_category: diagnostic`.

### §2.2 — Sleep segments (`sleep_analysis_latest`)

```yaml
state: "6h 19m"                           # totalSleep formatted (unchanged)
attributes:
  source_devices: ["Apple Watch"]
  date: "2026-05-24 00:00:00 -0700"
  time_asleep_min: 379
  time_in_bed_min: 477
  efficiency_pct: 79.5
  sleep_score: 62                         # null if Apple doesn't provide
  night_start: "2026-05-23T23:28:00-04:00"
  night_end: "2026-05-24T08:00:00-04:00"
  segments:
    - start: "2026-05-23T23:28:00-04:00"
      end: "2026-05-23T23:43:00-04:00"
      stage: "core"                       # awake | rem | core | deep
    - start: "2026-05-23T23:43:00-04:00"
      end: "2026-05-24T00:02:00-04:00"
      stage: "deep"
  lcars_schema_version: "1"
```

- Stage normalization: `AsleepCore`/`asleepCore` → `core`, `AsleepDeep` → `deep`, `AsleepREM` → `rem`, `Awake`/`InBed` → `awake`, `AsleepUnspecified` → `core`.
- Probes HAE keys: `sleepSegments`, `segments`, or `samples` for the segment array.
- Probes HAE keys: `inBed`/`timeInBed` for time-in-bed, `startDate`/`sleepStart` for night start, `endDate`/`sleepEnd` for night end, `sleepScore`/`score` for sleep score.
- If segments key not found in HAE data, `segments` attribute is **absent** (graceful degradation).

### §2.3 — Workout HR samples (`workout_last_avg_hr`)

```yaml
state: 142                                # average bpm (unchanged)
attributes:
  avg_bpm: 142
  max_bpm: 178
  duration_s: 2520
  workout_started: "2026-05-23T14:43:00-07:00"
  workout_type: "Other"
  samples:
    - { t_s: 0, bpm: 88 }
    - { t_s: 30, bpm: 95 }
    - { t_s: 60, bpm: 103 }
  lcars_schema_version: "1"
```

- `t_s` = seconds since workout start. `bpm` = rounded integer.
- Probes HAE keys: `heartRateData` or `heartRateSamples`.
- Within each sample, probes `date`/`timestamp` for time, `qty`/`Avg`/`bpm` for heart rate.
- Downsampled to ~30s cadence if >200 raw samples (keeps points ≥25s apart).
- If HR sample key not found in HAE data, `samples` attribute is **absent**.

### §2.4 — Workout GPS route (`workout_last_started`)

```yaml
state: "2026-05-23T14:43:00-07:00"       # ISO timestamp (unchanged)
attributes:
  workout_type: "Other"
  duration_s: 2520
  distance_m: 161
  route_compressed: "_p~iF~ps|U_ulLnnqC..."  # Google-encoded polyline
  lcars_schema_version: "1"
```

- Route points downsampled to ≤500 before encoding.
- Each point: probes `lat` and `lon`/`lng` from HAE's route array.
- Polyline encoder is built-in (no external dependency), verified against Google's reference test vector.
- If workout has no `route` key or route is empty, `route_compressed` is **absent**.
- **No raw coordinates are ever exposed** — only the encoded string.

### §2.5 — Schema probe

`lcars_schema_version: "1"` is present on all four entities above when their extended attributes are populated.

---

## Caveats for the LCARS renderer

1. **Sleep & workout HR are HAE-data-shape-dependent.** The probed key names (`sleepSegments`, `heartRateData`, etc.) are best-guesses from the HAE protocol docs. First live poll will confirm whether the attributes populate. If they don't, a one-line key-name fix is all that's needed — file an issue.

2. **Attribute truncation.** If any entity's JSON-serialized attributes exceed 40 KiB, `_safe_attr` replaces the **entire** attribute dict with the string `"(truncated — too large for entity attributes)"`. The renderer should treat `typeof attrs === 'string'` as a degraded-mode signal.

3. **ECG recorder exclusion.** The voltage array is on an `entity_category: diagnostic` sensor. HA's recorder will still persist attributes unless users add `sensor.*_ecg_voltage_measurements` to `recorder.exclude`. The Sickbay spec recommends this — consider documenting it in the LCARS setup guide.

4. **Route response size.** `includeRoutes: true` is now enabled for all workout requests. GPS-tracked workouts may return significantly larger TCP responses. The 4 MiB response cap handles this, but monitor for increased poll times on the workout coordinator (currently 10-min interval).

5. **Absent vs. null.** When source data is missing, attributes are **absent from the dict entirely** (not set to null). Check with `'voltage_uv' in attrs` / `hasattr`, not `attrs.voltage_uv !== null`.

---

## Testing checklist (from contract §4)

- [ ] ECG: take Apple Watch reading → waveform card renders within 30s
- [ ] Sleep: night of Apple Watch sleep → hypnogram appears with stage segments
- [ ] HR zones: workout >10 min with watch → 4 stacked bars proportional to zone time
- [ ] Route: outdoor GPS workout → SVG polyline from decoded `route_compressed`
- [ ] Schema probe: remove `lcars_schema_version` → renderer falls back to degraded mode
