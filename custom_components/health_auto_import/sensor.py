"""Sensors for Health Auto Import.

Each tool coordinator produces sensor entities for its data type per spec §3.
Sensors read from their coordinator's ``latest_records`` / ``data`` dict.

Security:
 - All server-returned strings pass through ``safe_slug`` before entity-ID use.
 - Attributes capped in size (no multi-MB voltage arrays in entity state).
 - No secrets or raw network data exposed in entity state.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    EntityCategory,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    TOOL_ECG,
    TOOL_HEALTH_METRICS,
    TOOL_HEART_NOTIFICATIONS,
    TOOL_MEDICATIONS,
    TOOL_WORKOUTS,
)
from .coordinator import (
    ReachabilityCoordinator,
    ToolCoordinator,
    parse_hae_ts,
    safe_slug,
)
from .entity import (
    DEVICE_HEALTH_METRICS,
    DEVICE_HEART,
    DEVICE_MEDICATIONS,
    DEVICE_SERVER,
    DEVICE_WORKOUTS,
    HaeEntity,
)

_LOGGER = logging.getLogger(__name__)

# Maximum attribute payload size (bytes) to prevent state-event bloat.
# Raised from 16 KiB to 40 KiB for ECG voltage arrays (512 Hz × 30 s ≈ 30 KB).
_MAX_ATTR_BYTES = 40_960


def _encode_polyline(coords: list[tuple[float, float]]) -> str:
    """Google-encode a list of (lat, lng) tuples into a polyline string."""
    result: list[str] = []
    prev_lat = 0
    prev_lng = 0
    for lat, lng in coords:
        lat_e5 = round(lat * 1e5)
        lng_e5 = round(lng * 1e5)
        d_lat = lat_e5 - prev_lat
        d_lng = lng_e5 - prev_lng
        prev_lat = lat_e5
        prev_lng = lng_e5
        for val in (d_lat, d_lng):
            val = ~(val << 1) if val < 0 else val << 1
            while val >= 0x20:
                result.append(chr((0x20 | (val & 0x1F)) + 63))
                val >>= 5
            result.append(chr(val + 63))
    return "".join(result)


def _safe_attr(data: Any) -> Any:
    """Truncate oversized attribute payloads."""
    import json as _json

    try:
        encoded = _json.dumps(data, default=str)
    except (TypeError, ValueError):
        return None
    if len(encoded) > _MAX_ATTR_BYTES:
        return "(truncated — too large for entity attributes)"
    return data


# ---------------------------------------------------------------------------
# Entity setup
# ---------------------------------------------------------------------------


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    host: str = entry.data[CONF_HOST]
    eid = entry.entry_id

    entities: list[SensorEntity] = []

    # Reachability diagnostics.
    reachability: ReachabilityCoordinator = data["reachability"]
    entities.append(LastProbeSensor(reachability, entry_id=eid, host=host))
    entities.append(
        ServerStatusSensor(reachability, data["coordinators"], entry_id=eid, host=host)
    )
    entities.append(ConsecutiveFailuresSensor(reachability, data["coordinators"],
                                              entry_id=eid, host=host))

    # Per-tool sensors.
    coordinators: dict[str, ToolCoordinator] = data.get("coordinators", {})
    for tool_name, coord in coordinators.items():
        if tool_name == TOOL_ECG:
            entities.extend(_ecg_sensors(coord, eid, host))
        elif tool_name == TOOL_HEART_NOTIFICATIONS:
            entities.extend(_hrn_sensors(coord, eid, host))
        elif tool_name == TOOL_WORKOUTS:
            entities.extend(_workout_sensors(coord, eid, host))
        elif tool_name == TOOL_HEALTH_METRICS:
            entities.extend(_metric_sensors(coord, eid, host, data.get("discovered_metrics", [])))
        elif tool_name == TOOL_MEDICATIONS:
            entities.extend(_medication_sensors(coord, eid, host))

    async_add_entities(entities)


# ---------------------------------------------------------------------------
# Reachability / status sensors
# ---------------------------------------------------------------------------


class LastProbeSensor(HaeEntity, SensorEntity):
    """Diagnostic: timestamp of the last successful reachability probe."""

    _attr_name = "Last probe"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: ReachabilityCoordinator, *, entry_id: str, host: str) -> None:
        super().__init__(coordinator, entry_id=entry_id, host=host, unique_suffix="last_probe",
                         device_group=DEVICE_SERVER)

    @property
    def native_value(self) -> dt.datetime | None:
        return self.coordinator.last_probe_time


class ServerStatusSensor(HaeEntity, SensorEntity):
    """Coarse server status: online / degraded / unreachable."""

    _attr_name = "Server status"

    def __init__(
        self,
        reachability: ReachabilityCoordinator,
        tool_coordinators: dict[str, ToolCoordinator],
        *,
        entry_id: str,
        host: str,
    ) -> None:
        super().__init__(reachability, entry_id=entry_id, host=host, unique_suffix="status",
                         device_group=DEVICE_SERVER)
        self._tool_coordinators = tool_coordinators

    @property
    def native_value(self) -> str:
        if not self.coordinator.last_update_success or not self.coordinator.data:
            return "unreachable"
        any_failed = any(
            not c.last_update_success for c in self._tool_coordinators.values()
        )
        return "degraded" if any_failed else "online"


class ConsecutiveFailuresSensor(HaeEntity, SensorEntity):
    """Count of consecutive coordinator failures across all tools."""

    _attr_name = "Consecutive failures"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        reachability: ReachabilityCoordinator,
        tool_coordinators: dict[str, ToolCoordinator],
        *,
        entry_id: str,
        host: str,
    ) -> None:
        super().__init__(reachability, entry_id=entry_id, host=host,
                         unique_suffix="consecutive_failures",
                         device_group=DEVICE_SERVER)
        self._tool_coordinators = tool_coordinators

    @property
    def native_value(self) -> int:
        return sum(c.consecutive_failures for c in self._tool_coordinators.values())


# ---------------------------------------------------------------------------
# ECG sensors (§3.1)
# ---------------------------------------------------------------------------


def _ecg_sensors(coord: ToolCoordinator, eid: str, host: str) -> list[SensorEntity]:
    return [
        _EcgClassificationSensor(coord, entry_id=eid, host=host),
        _EcgAvgBpmSensor(coord, entry_id=eid, host=host),
        _EcgTakenAtSensor(coord, entry_id=eid, host=host),
        _EcgDurationSensor(coord, entry_id=eid, host=host),
        _EcgSamplingHzSensor(coord, entry_id=eid, host=host),
        _EcgVoltageCountSensor(coord, entry_id=eid, host=host),
    ]


def _latest_ecg(coord: ToolCoordinator) -> dict[str, Any] | None:
    records = coord.latest_records
    if not records:
        return None
    # Return the most recent by start time.
    return max(records, key=lambda r: r.get("start", ""), default=None)


class _EcgClassificationSensor(HaeEntity, SensorEntity):
    _attr_name = "ECG classification"

    def __init__(self, coord: ToolCoordinator, *, entry_id: str, host: str) -> None:
        super().__init__(coord, entry_id=entry_id, host=host,
                         unique_suffix="ecg_last_classification",
                         device_group=DEVICE_HEART)

    @property
    def native_value(self) -> str | None:
        rec = _latest_ecg(self.coordinator)  # type: ignore[arg-type]
        return rec.get("classification") if rec else None


class _EcgAvgBpmSensor(HaeEntity, SensorEntity):
    _attr_name = "ECG average BPM"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "bpm"

    def __init__(self, coord: ToolCoordinator, *, entry_id: str, host: str) -> None:
        super().__init__(coord, entry_id=entry_id, host=host,
                         unique_suffix="ecg_last_average_bpm",
                         device_group=DEVICE_HEART)

    @property
    def native_value(self) -> int | None:
        rec = _latest_ecg(self.coordinator)  # type: ignore[arg-type]
        val = rec.get("averageHeartRate") if rec else None
        return int(val) if val is not None else None


class _EcgTakenAtSensor(HaeEntity, SensorEntity):
    _attr_name = "ECG last taken"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coord: ToolCoordinator, *, entry_id: str, host: str) -> None:
        super().__init__(coord, entry_id=entry_id, host=host,
                         unique_suffix="ecg_last_taken_at",
                         device_group=DEVICE_HEART)

    @property
    def native_value(self) -> dt.datetime | None:
        rec = _latest_ecg(self.coordinator)  # type: ignore[arg-type]
        return parse_hae_ts(rec.get("start", "")) if rec else None


class _EcgDurationSensor(HaeEntity, SensorEntity):
    _attr_name = "ECG duration"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coord: ToolCoordinator, *, entry_id: str, host: str) -> None:
        super().__init__(coord, entry_id=entry_id, host=host,
                         unique_suffix="ecg_last_duration_s",
                         device_group=DEVICE_HEART)

    @property
    def native_value(self) -> float | None:
        rec = _latest_ecg(self.coordinator)  # type: ignore[arg-type]
        if not rec:
            return None
        start = parse_hae_ts(rec.get("start", ""))
        end = parse_hae_ts(rec.get("end", ""))
        if start and end:
            return (end - start).total_seconds()
        return None


class _EcgSamplingHzSensor(HaeEntity, SensorEntity):
    _attr_name = "ECG sampling frequency"
    _attr_native_unit_of_measurement = "Hz"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord: ToolCoordinator, *, entry_id: str, host: str) -> None:
        super().__init__(coord, entry_id=entry_id, host=host,
                         unique_suffix="ecg_last_sampling_frequency_hz",
                         device_group=DEVICE_HEART)

    @property
    def native_value(self) -> int | None:
        rec = _latest_ecg(self.coordinator)  # type: ignore[arg-type]
        return rec.get("samplingFrequency") if rec else None


class _EcgVoltageCountSensor(HaeEntity, SensorEntity):
    _attr_name = "ECG voltage measurements"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord: ToolCoordinator, *, entry_id: str, host: str) -> None:
        super().__init__(coord, entry_id=entry_id, host=host,
                         unique_suffix="ecg_last_voltage_count",
                         device_group=DEVICE_HEART)

    @property
    def native_value(self) -> int | None:
        rec = _latest_ecg(self.coordinator)  # type: ignore[arg-type]
        return rec.get("numberOfVoltageMeasurements") if rec else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        rec = _latest_ecg(self.coordinator)  # type: ignore[arg-type]
        if not rec:
            return None
        raw = rec.get("voltageMeasurements")
        if not isinstance(raw, list) or not raw:
            return None
        # Extract microvolt values from [{date, units, voltage}, ...].
        voltage_uv = [
            round(m["voltage"]) if isinstance(m.get("voltage"), (int, float)) else m.get("voltage")
            for m in raw
        ]
        attrs: dict[str, Any] = {
            "voltage_uv": voltage_uv,
            "voltage_unit": "uV",
            "classification": rec.get("classification"),
            "average_bpm": (int(v) if (v := rec.get("averageHeartRate")) is not None else None),
            "duration_s": None,
            "sampling_frequency_hz": rec.get("samplingFrequency"),
            "lcars_schema_version": "1",
        }
        start = parse_hae_ts(rec.get("start", ""))
        end = parse_hae_ts(rec.get("end", ""))
        if start and end:
            attrs["duration_s"] = (end - start).total_seconds()
        if start:
            attrs["recorded_at"] = start.isoformat()
        return _safe_attr(attrs)


# ---------------------------------------------------------------------------
# Heart-rate notification sensors (§3.2)
# ---------------------------------------------------------------------------


def _hrn_sensors(coord: ToolCoordinator, eid: str, host: str) -> list[SensorEntity]:
    return [
        _HrnLastKindSensor(coord, entry_id=eid, host=host),
        _HrnLastAtSensor(coord, entry_id=eid, host=host),
        _HrnCount7dSensor(coord, entry_id=eid, host=host),
    ]


def _latest_hrn(coord: ToolCoordinator) -> dict[str, Any] | None:
    records = coord.latest_records
    return max(records, key=lambda r: r.get("start", ""), default=None) if records else None


class _HrnLastKindSensor(HaeEntity, SensorEntity):
    _attr_name = "Heart notification last kind"

    def __init__(self, coord: ToolCoordinator, *, entry_id: str, host: str) -> None:
        super().__init__(coord, entry_id=entry_id, host=host,
                         unique_suffix="hrn_last_kind",
                         device_group=DEVICE_HEART)

    @property
    def native_value(self) -> str | None:
        rec = _latest_hrn(self.coordinator)  # type: ignore[arg-type]
        if not rec:
            return None
        return rec.get("type") or rec.get("kind")


class _HrnLastAtSensor(HaeEntity, SensorEntity):
    _attr_name = "Heart notification last event"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coord: ToolCoordinator, *, entry_id: str, host: str) -> None:
        super().__init__(coord, entry_id=entry_id, host=host,
                         unique_suffix="hrn_last_at",
                         device_group=DEVICE_HEART)

    @property
    def native_value(self) -> dt.datetime | None:
        rec = _latest_hrn(self.coordinator)  # type: ignore[arg-type]
        return parse_hae_ts(rec.get("start", "")) if rec else None


class _HrnCount7dSensor(HaeEntity, SensorEntity):
    _attr_name = "Heart notifications (7d)"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coord: ToolCoordinator, *, entry_id: str, host: str) -> None:
        super().__init__(coord, entry_id=entry_id, host=host,
                         unique_suffix="hrn_count_7d",
                         device_group=DEVICE_HEART)

    @property
    def native_value(self) -> int:
        return len(self.coordinator.latest_records) if self.coordinator.latest_records else 0  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Workout sensors (§3.3)
# ---------------------------------------------------------------------------


def _workout_sensors(coord: ToolCoordinator, eid: str, host: str) -> list[SensorEntity]:
    return [
        _WorkoutTypeSensor(coord, entry_id=eid, host=host),
        _WorkoutStartedAtSensor(coord, entry_id=eid, host=host),
        _WorkoutDurationSensor(coord, entry_id=eid, host=host),
        _WorkoutEnergySensor(coord, entry_id=eid, host=host),
        _WorkoutAvgHrSensor(coord, entry_id=eid, host=host),
        _WorkoutMaxHrSensor(coord, entry_id=eid, host=host),
        _WorkoutCount7dSensor(coord, entry_id=eid, host=host),
    ]


def _latest_workout(coord: ToolCoordinator) -> dict[str, Any] | None:
    records = coord.latest_records
    return max(records, key=lambda r: r.get("start", ""), default=None) if records else None


class _WorkoutTypeSensor(HaeEntity, SensorEntity):
    _attr_name = "Workout last type"

    def __init__(self, coord: ToolCoordinator, *, entry_id: str, host: str) -> None:
        super().__init__(coord, entry_id=entry_id, host=host,
                         unique_suffix="workout_last_type",
                         device_group=DEVICE_WORKOUTS)

    @property
    def native_value(self) -> str | None:
        rec = _latest_workout(self.coordinator)  # type: ignore[arg-type]
        return rec.get("name") if rec else None


class _WorkoutStartedAtSensor(HaeEntity, SensorEntity):
    _attr_name = "Workout last started"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coord: ToolCoordinator, *, entry_id: str, host: str) -> None:
        super().__init__(coord, entry_id=entry_id, host=host,
                         unique_suffix="workout_last_started_at",
                         device_group=DEVICE_WORKOUTS)

    @property
    def native_value(self) -> dt.datetime | None:
        rec = _latest_workout(self.coordinator)  # type: ignore[arg-type]
        return parse_hae_ts(rec.get("start", "")) if rec else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        rec = _latest_workout(self.coordinator)  # type: ignore[arg-type]
        if not rec:
            return None
        attrs: dict[str, Any] = {
            "workout_type": rec.get("name"),
            "duration_s": rec.get("duration"),
            "lcars_schema_version": "1",
        }
        dist = rec.get("distance")
        if isinstance(dist, dict):
            attrs["distance_m"] = dist.get("qty")
        elif isinstance(dist, (int, float)):
            attrs["distance_m"] = dist
        # GPS route → Google-encoded polyline (§2.4 LCARS data contract).
        route = rec.get("route") or []
        if isinstance(route, list) and route:
            coords: list[tuple[float, float]] = []
            for pt in route:
                lat = pt.get("lat")
                lon = pt.get("lon") or pt.get("lng")
                if lat is not None and lon is not None:
                    coords.append((float(lat), float(lon)))
            if coords:
                # Downsample to ≤500 points.
                if len(coords) > 500:
                    step = len(coords) / 500
                    coords = [coords[round(i * step)] for i in range(500)]
                attrs["route_compressed"] = _encode_polyline(coords)
        return _safe_attr(attrs)


class _WorkoutDurationSensor(HaeEntity, SensorEntity):
    _attr_name = "Workout last duration"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coord: ToolCoordinator, *, entry_id: str, host: str) -> None:
        super().__init__(coord, entry_id=entry_id, host=host,
                         unique_suffix="workout_last_duration_min",
                         device_group=DEVICE_WORKOUTS)

    @property
    def native_value(self) -> float | None:
        rec = _latest_workout(self.coordinator)  # type: ignore[arg-type]
        dur = rec.get("duration") if rec else None
        return round(dur / 60, 1) if dur is not None else None


class _WorkoutEnergySensor(HaeEntity, SensorEntity):
    _attr_name = "Workout last energy"
    _attr_native_unit_of_measurement = "kcal"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coord: ToolCoordinator, *, entry_id: str, host: str) -> None:
        super().__init__(coord, entry_id=entry_id, host=host,
                         unique_suffix="workout_last_active_energy_kcal",
                         device_group=DEVICE_WORKOUTS)

    @property
    def native_value(self) -> float | None:
        rec = _latest_workout(self.coordinator)  # type: ignore[arg-type]
        if not rec:
            return None
        aeb = rec.get("activeEnergyBurned")
        if isinstance(aeb, dict):
            return aeb.get("qty")
        return aeb


class _WorkoutAvgHrSensor(HaeEntity, SensorEntity):
    _attr_name = "Workout last avg HR"
    _attr_native_unit_of_measurement = "bpm"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coord: ToolCoordinator, *, entry_id: str, host: str) -> None:
        super().__init__(coord, entry_id=entry_id, host=host,
                         unique_suffix="workout_last_avg_hr_bpm",
                         device_group=DEVICE_WORKOUTS)

    @property
    def native_value(self) -> int | None:
        rec = _latest_workout(self.coordinator)  # type: ignore[arg-type]
        if not rec:
            return None
        hr = rec.get("heartRate")
        if isinstance(hr, dict):
            avg = hr.get("avg")
            if isinstance(avg, dict):
                val = avg.get("qty")
                return int(val) if val is not None else None
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        rec = _latest_workout(self.coordinator)  # type: ignore[arg-type]
        if not rec:
            return None
        attrs: dict[str, Any] = {"lcars_schema_version": "1"}
        hr = rec.get("heartRate")
        if isinstance(hr, dict):
            avg = hr.get("avg")
            mx = hr.get("max")
            attrs["avg_bpm"] = int(avg["qty"]) if isinstance(avg, dict) and avg.get("qty") is not None else None
            attrs["max_bpm"] = int(mx["qty"]) if isinstance(mx, dict) and mx.get("qty") is not None else None
        attrs["duration_s"] = rec.get("duration")
        start_ts = parse_hae_ts(rec.get("start", ""))
        attrs["workout_started"] = start_ts.isoformat() if start_ts else None
        attrs["workout_type"] = rec.get("name")
        # HR time-series samples (§2.3 LCARS data contract).
        raw_hr = rec.get("heartRateData") or rec.get("heartRateSamples") or []
        if isinstance(raw_hr, list) and raw_hr and start_ts:
            start_epoch = start_ts.timestamp()
            samples: list[dict[str, int | float]] = []
            for pt in raw_hr:
                ts = pt.get("date") or pt.get("timestamp")
                bpm = pt.get("qty") or pt.get("Avg") or pt.get("bpm")
                if ts is not None and bpm is not None:
                    if isinstance(ts, (int, float)):
                        t_s = round(ts - start_epoch)
                    else:
                        parsed = parse_hae_ts(str(ts))
                        t_s = round(parsed.timestamp() - start_epoch) if parsed else None
                    if t_s is not None:
                        samples.append({"t_s": t_s, "bpm": round(bpm)})
            # Downsample to ~30s cadence if dense.
            if len(samples) > 1:
                samples.sort(key=lambda s: s["t_s"])
                if len(samples) > 200:
                    downsampled = [samples[0]]
                    for s in samples[1:]:
                        if s["t_s"] - downsampled[-1]["t_s"] >= 25:
                            downsampled.append(s)
                    samples = downsampled
            attrs["samples"] = samples if samples else None
        return _safe_attr(attrs)


class _WorkoutMaxHrSensor(HaeEntity, SensorEntity):
    _attr_name = "Workout last max HR"
    _attr_native_unit_of_measurement = "bpm"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coord: ToolCoordinator, *, entry_id: str, host: str) -> None:
        super().__init__(coord, entry_id=entry_id, host=host,
                         unique_suffix="workout_last_max_hr_bpm",
                         device_group=DEVICE_WORKOUTS)

    @property
    def native_value(self) -> int | None:
        rec = _latest_workout(self.coordinator)  # type: ignore[arg-type]
        if not rec:
            return None
        hr = rec.get("heartRate")
        if isinstance(hr, dict):
            mx = hr.get("max")
            if isinstance(mx, dict):
                val = mx.get("qty")
                return int(val) if val is not None else None
        return None


class _WorkoutCount7dSensor(HaeEntity, SensorEntity):
    _attr_name = "Workouts (7d)"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coord: ToolCoordinator, *, entry_id: str, host: str) -> None:
        super().__init__(coord, entry_id=entry_id, host=host,
                         unique_suffix="workout_count_7d",
                         device_group=DEVICE_WORKOUTS)

    @property
    def native_value(self) -> int:
        return len(self.coordinator.latest_records) if self.coordinator.latest_records else 0  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Health-metrics sensors (§3.4) — dynamically created per discovered metric
# ---------------------------------------------------------------------------


def _metric_sensors(
    coord: ToolCoordinator, eid: str, host: str, discovered_metrics: list[str]
) -> list[SensorEntity]:
    entities: list[SensorEntity] = []
    for metric_name in discovered_metrics:
        slug = safe_slug(metric_name)
        entities.append(_MetricLatestSensor(coord, metric_name=metric_name,
                                            entry_id=eid, host=host, slug=slug))
        # Add daily-total sensor for cumulative metrics.
        if slug in _MetricDailyTotalSensor.CUMULATIVE_METRICS:
            entities.append(_MetricDailyTotalSensor(coord, metric_name=metric_name,
                                                     entry_id=eid, host=host, slug=slug))
    return entities


class _MetricLatestSensor(HaeEntity, SensorEntity):
    """Latest value for a single health_metrics metric."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    # Metrics that should be displayed as integers.
    _INTEGER_METRICS = frozenset({
        "apple_stand_hour", "flights_climbed", "heart_rate",
        "step_count", "resting_heart_rate", "walking_heart_rate_average",
        "six_minute_walking_test_distance",
    })

    # Metrics that return composite/string values (no state_class).
    _COMPOSITE_METRICS = frozenset({"blood_pressure", "sleep_analysis"})

    # Rounding precision per metric (default is 1 decimal).
    _PRECISION: dict[str, int] = {
        "blood_oxygen_saturation": 0,
        "walking_asymmetry_percentage": 0,
        "walking_double_support_percentage": 1,
        "heart_rate_variability": 1,
        "respiratory_rate": 1,
        "vo2_max": 1,
        "apple_sleeping_wrist_temperature": 1,
        "active_energy": 1,
        "basal_energy_burned": 1,
        "physical_effort": 1,
        "walking_speed": 1,
        "walking_step_length": 1,
        "stair_speed_down": 2,
        "stair_speed_up": 2,
        "walking_running_distance": 2,
        "environmental_audio_exposure": 1,
        "headphone_audio_exposure": 1,
        "cardio_recovery": 1,
        "breathing_disturbances": 1,
    }

    # Unit cleanup: server unit → display unit.
    # Only cosmetic normalisations that don't break recorder statistics.
    _UNIT_MAP: dict[str, str] = {
        "degF": "°F",
        "degC": "°C",
    }

    def __init__(
        self,
        coord: ToolCoordinator,
        *,
        metric_name: str,
        entry_id: str,
        host: str,
        slug: str,
    ) -> None:
        super().__init__(coord, entry_id=entry_id, host=host,
                         unique_suffix=f"metric_{slug}_latest",
                         device_group=DEVICE_HEALTH_METRICS)
        self._metric_name = metric_name
        self._slug = slug
        self._attr_name = f"{metric_name.replace('_', ' ').title()} (latest)"
        if slug in self._COMPOSITE_METRICS:
            self._attr_state_class = None

    def _round(self, value: float) -> float | int:
        """Round a numeric value based on metric type."""
        if self._slug in self._INTEGER_METRICS:
            return int(round(value))
        precision = self._PRECISION.get(self._slug, 1)
        return round(value, precision)

    @property
    def native_value(self) -> float | int | str | None:
        records = self.coordinator.latest_records  # type: ignore[union-attr]
        if not records:
            return None
        for bucket in records:
            if bucket.get("name") == self._metric_name:
                points = bucket.get("data")
                if isinstance(points, list) and points:
                    latest = max(points, key=lambda p: p.get("date", ""))
                    # Composite: blood_pressure
                    if "systolic" in latest and "diastolic" in latest:
                        sys_v = round(latest["systolic"])
                        dia_v = round(latest["diastolic"])
                        return f"{sys_v}/{dia_v}"
                    # Composite: sleep_analysis (totalSleep is in hours)
                    if "totalSleep" in latest:
                        hours = latest["totalSleep"]
                        h = int(hours)
                        m = int(round((hours - h) * 60))
                        return f"{h}h {m}m"
                    # Standard scalar value.
                    if "qty" in latest:
                        val = latest["qty"]
                        if isinstance(val, (int, float)):
                            return self._round(val)
                        return val
                    # Aggregated metrics (e.g. heart_rate) use Avg/Min/Max.
                    if "Avg" in latest:
                        val = latest["Avg"]
                        if isinstance(val, (int, float)):
                            return self._round(val)
                        return val
        return None

    @property
    def native_unit_of_measurement(self) -> str | None:
        # Composite metrics that return strings don't have a single unit.
        if self._slug in ("blood_pressure", "sleep_analysis"):
            return None
        records = self.coordinator.latest_records  # type: ignore[union-attr]
        if not records:
            return None
        for bucket in records:
            if bucket.get("name") == self._metric_name:
                units = bucket.get("units", "")
                if not units:
                    return None
                # Clean up server unit names.
                return self._UNIT_MAP.get(units, units)
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        records = self.coordinator.latest_records  # type: ignore[union-attr]
        if not records:
            return None
        for bucket in records:
            if bucket.get("name") == self._metric_name:
                points = bucket.get("data")
                if isinstance(points, list) and points:
                    latest = max(points, key=lambda p: p.get("date", ""))
                    source = latest.get("source", "")
                    attrs: dict[str, Any] = {}
                    if source:
                        attrs["source_devices"] = [s.strip() for s in source.split("|")]
                    attrs["date"] = latest.get("date")
                    # Include min/max for aggregated metrics (e.g. heart_rate).
                    if "Min" in latest:
                        attrs["min"] = latest["Min"]
                    if "Max" in latest:
                        attrs["max"] = latest["Max"]
                    # Sleep-analysis segments (§2.2 LCARS data contract).
                    if self._slug == "sleep_analysis":
                        attrs["lcars_schema_version"] = "1"
                        total = latest.get("totalSleep")
                        if total is not None:
                            attrs["time_asleep_min"] = round(total * 60)
                        in_bed = latest.get("inBed") or latest.get("timeInBed")
                        if in_bed is not None:
                            attrs["time_in_bed_min"] = round(float(in_bed) * 60)
                            if total and in_bed:
                                attrs["efficiency_pct"] = round(total / float(in_bed) * 100, 1)
                        attrs["sleep_score"] = latest.get("sleepScore") or latest.get("score")
                        attrs["night_start"] = latest.get("startDate") or latest.get("sleepStart")
                        attrs["night_end"] = latest.get("endDate") or latest.get("sleepEnd")
                        # Stage segments from Apple Health sleep analysis.
                        _STAGE_MAP = {
                            "InBed": "awake", "inBed": "awake",
                            "Awake": "awake", "awake": "awake",
                            "AsleepCore": "core", "asleepCore": "core", "core": "core",
                            "AsleepDeep": "deep", "asleepDeep": "deep", "deep": "deep",
                            "AsleepREM": "rem", "asleepREM": "rem", "rem": "rem",
                            "AsleepUnspecified": "core", "asleepUnspecified": "core",
                        }
                        raw_segs = (
                            latest.get("sleepSegments")
                            or latest.get("segments")
                            or latest.get("samples")
                        )
                        if isinstance(raw_segs, list) and raw_segs:
                            segments = []
                            for seg in raw_segs:
                                stage_raw = seg.get("stage") or seg.get("value") or seg.get("sleepStage") or ""
                                stage = _STAGE_MAP.get(stage_raw, stage_raw.lower() if stage_raw else "")
                                if stage:
                                    segments.append({
                                        "start": seg.get("start") or seg.get("startDate"),
                                        "end": seg.get("end") or seg.get("endDate"),
                                        "stage": stage,
                                    })
                            if segments:
                                attrs["segments"] = segments
                    return _safe_attr(attrs)
        return None


class _MetricDailyTotalSensor(HaeEntity, SensorEntity):
    """Daily sum for cumulative health_metrics (steps, calories, etc.).

    Unlike the ``(latest)`` sensor which shows the most recent data point
    (e.g. "83 steps in the last 15 min"), this sums ALL data points whose
    date falls on today, matching what Apple Health / Oura / Withings show
    as "Steps today".
    """

    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    # Metrics where a daily sum makes sense (additive quantities).
    CUMULATIVE_METRICS = frozenset({
        "step_count",
        "active_energy",
        "basal_energy_burned",
        "flights_climbed",
        "apple_exercise_time",
        "apple_stand_hour",
        "apple_stand_time",
        "walking_running_distance",
    })

    # Integer display.
    _INTEGER = frozenset({
        "step_count", "flights_climbed", "apple_stand_hour",
        "apple_exercise_time", "apple_stand_time",
    })

    # Unit cleanup.
    _UNIT_MAP: dict[str, str] = {"degF": "°F", "degC": "°C"}

    def __init__(
        self,
        coord: ToolCoordinator,
        *,
        metric_name: str,
        entry_id: str,
        host: str,
        slug: str,
    ) -> None:
        super().__init__(coord, entry_id=entry_id, host=host,
                         unique_suffix=f"metric_{slug}_daily_total",
                         device_group=DEVICE_HEALTH_METRICS)
        self._metric_name = metric_name
        self._slug = slug
        self._attr_name = f"{metric_name.replace('_', ' ').title()} (today)"

    def _today_str(self) -> str:
        """Return today's date as YYYY-MM-DD in the local timezone."""
        return dt_util.now().strftime("%Y-%m-%d")

    @property
    def native_value(self) -> float | int | None:
        records = self.coordinator.latest_records  # type: ignore[union-attr]
        if not records:
            return None
        today = self._today_str()
        for bucket in records:
            if bucket.get("name") == self._metric_name:
                points = bucket.get("data")
                if not isinstance(points, list):
                    return None
                total = 0.0
                found = False
                for pt in points:
                    date_str = pt.get("date", "")
                    if not date_str.startswith(today):
                        continue
                    val = pt.get("qty")
                    if isinstance(val, (int, float)):
                        total += val
                        found = True
                if not found:
                    return None
                if self._slug in self._INTEGER:
                    return int(round(total))
                return round(total, 1)
        return None

    @property
    def native_unit_of_measurement(self) -> str | None:
        records = self.coordinator.latest_records  # type: ignore[union-attr]
        if not records:
            return None
        for bucket in records:
            if bucket.get("name") == self._metric_name:
                units = bucket.get("units", "")
                return self._UNIT_MAP.get(units, units) if units else None
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        records = self.coordinator.latest_records  # type: ignore[union-attr]
        if not records:
            return None
        today = self._today_str()
        for bucket in records:
            if bucket.get("name") == self._metric_name:
                points = bucket.get("data")
                if not isinstance(points, list):
                    return None
                today_points = [p for p in points if p.get("date", "").startswith(today)]
                if today_points:
                    return _safe_attr({
                        "data_points_today": len(today_points),
                        "date": today,
                    })
        return None


# ---------------------------------------------------------------------------
# Medication sensors (§3.5)
# ---------------------------------------------------------------------------


def _medication_sensors(
    coord: ToolCoordinator, eid: str, host: str
) -> list[SensorEntity]:
    """Create one status sensor per coordinator.

    Per-medication entity splitting happens dynamically when the coordinator
    ingests dose records — for beta.1 we expose the last overall status.
    """
    return [
        _MedicationLastStatusSensor(coord, entry_id=eid, host=host),
        _MedicationLastScheduledAtSensor(coord, entry_id=eid, host=host),
    ]


def _latest_medication(coord: ToolCoordinator) -> dict[str, Any] | None:
    records = coord.latest_records
    return max(records, key=lambda r: r.get("scheduledDate", ""), default=None) if records else None


class _MedicationLastStatusSensor(HaeEntity, SensorEntity):
    _attr_name = "Medication last status"

    def __init__(self, coord: ToolCoordinator, *, entry_id: str, host: str) -> None:
        super().__init__(coord, entry_id=entry_id, host=host,
                         unique_suffix="med_last_status",
                         device_group=DEVICE_MEDICATIONS)

    @property
    def native_value(self) -> str | None:
        rec = _latest_medication(self.coordinator)  # type: ignore[arg-type]
        return rec.get("status") if rec else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        rec = _latest_medication(self.coordinator)  # type: ignore[arg-type]
        if not rec:
            return None
        return _safe_attr({
            "display_text": rec.get("displayText"),
            "scheduled_date": rec.get("scheduledDate"),
            "dosage": rec.get("scheduledDosage"),
            "units": rec.get("units"),
        })


class _MedicationLastScheduledAtSensor(HaeEntity, SensorEntity):
    _attr_name = "Medication last scheduled"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coord: ToolCoordinator, *, entry_id: str, host: str) -> None:
        super().__init__(coord, entry_id=entry_id, host=host,
                         unique_suffix="med_last_scheduled_at",
                         device_group=DEVICE_MEDICATIONS)

    @property
    def native_value(self) -> dt.datetime | None:
        rec = _latest_medication(self.coordinator)  # type: ignore[arg-type]
        return parse_hae_ts(rec.get("scheduledDate", "")) if rec else None
