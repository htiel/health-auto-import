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
from .entity import HaeEntity

_LOGGER = logging.getLogger(__name__)

# Maximum attribute payload size (bytes) to prevent state-event bloat.
_MAX_ATTR_BYTES = 16_384


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
        super().__init__(coordinator, entry_id=entry_id, host=host, unique_suffix="last_probe")

    @property
    def native_value(self) -> dt.datetime | None:
        return self.coordinator.last_update_success_time


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
        super().__init__(reachability, entry_id=entry_id, host=host, unique_suffix="status")
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
                         unique_suffix="consecutive_failures")
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
                         unique_suffix="ecg_last_classification")

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
                         unique_suffix="ecg_last_average_bpm")

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
                         unique_suffix="ecg_last_taken_at")

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
                         unique_suffix="ecg_last_duration_s")

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
                         unique_suffix="ecg_last_sampling_frequency_hz")

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
                         unique_suffix="ecg_last_voltage_count")

    @property
    def native_value(self) -> int | None:
        rec = _latest_ecg(self.coordinator)  # type: ignore[arg-type]
        return rec.get("numberOfVoltageMeasurements") if rec else None


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
                         unique_suffix="hrn_last_kind")

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
                         unique_suffix="hrn_last_at")

    @property
    def native_value(self) -> dt.datetime | None:
        rec = _latest_hrn(self.coordinator)  # type: ignore[arg-type]
        return parse_hae_ts(rec.get("start", "")) if rec else None


class _HrnCount7dSensor(HaeEntity, SensorEntity):
    _attr_name = "Heart notifications (7d)"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coord: ToolCoordinator, *, entry_id: str, host: str) -> None:
        super().__init__(coord, entry_id=entry_id, host=host,
                         unique_suffix="hrn_count_7d")

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
                         unique_suffix="workout_last_type")

    @property
    def native_value(self) -> str | None:
        rec = _latest_workout(self.coordinator)  # type: ignore[arg-type]
        return rec.get("name") if rec else None


class _WorkoutStartedAtSensor(HaeEntity, SensorEntity):
    _attr_name = "Workout last started"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coord: ToolCoordinator, *, entry_id: str, host: str) -> None:
        super().__init__(coord, entry_id=entry_id, host=host,
                         unique_suffix="workout_last_started_at")

    @property
    def native_value(self) -> dt.datetime | None:
        rec = _latest_workout(self.coordinator)  # type: ignore[arg-type]
        return parse_hae_ts(rec.get("start", "")) if rec else None


class _WorkoutDurationSensor(HaeEntity, SensorEntity):
    _attr_name = "Workout last duration"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coord: ToolCoordinator, *, entry_id: str, host: str) -> None:
        super().__init__(coord, entry_id=entry_id, host=host,
                         unique_suffix="workout_last_duration_min")

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
                         unique_suffix="workout_last_active_energy_kcal")

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
                         unique_suffix="workout_last_avg_hr_bpm")

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


class _WorkoutMaxHrSensor(HaeEntity, SensorEntity):
    _attr_name = "Workout last max HR"
    _attr_native_unit_of_measurement = "bpm"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coord: ToolCoordinator, *, entry_id: str, host: str) -> None:
        super().__init__(coord, entry_id=entry_id, host=host,
                         unique_suffix="workout_last_max_hr_bpm")

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
                         unique_suffix="workout_count_7d")

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
    return entities


class _MetricLatestSensor(HaeEntity, SensorEntity):
    """Latest value for a single health_metrics metric."""

    _attr_state_class = SensorStateClass.MEASUREMENT

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
                         unique_suffix=f"metric_{slug}_latest")
        self._metric_name = metric_name
        self._attr_name = f"{metric_name.replace('_', ' ').title()} (latest)"

    @property
    def native_value(self) -> float | str | None:
        records = self.coordinator.latest_records  # type: ignore[union-attr]
        if not records:
            return None
        for bucket in records:
            if bucket.get("name") == self._metric_name:
                points = bucket.get("data")
                if isinstance(points, list) and points:
                    latest = max(points, key=lambda p: p.get("date", ""))
                    # Composite metrics (blood_pressure, sleep_analysis) have nested shape.
                    if "qty" in latest:
                        return latest["qty"]
                    if "systolic" in latest and "diastolic" in latest:
                        return f"{latest['systolic']}/{latest['diastolic']}"
                    if "totalSleep" in latest:
                        return round(latest["totalSleep"] * 60, 1)
        return None

    @property
    def native_unit_of_measurement(self) -> str | None:
        records = self.coordinator.latest_records  # type: ignore[union-attr]
        if not records:
            return None
        for bucket in records:
            if bucket.get("name") == self._metric_name:
                units = bucket.get("units", "")
                return units if units else None
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
                    return _safe_attr(attrs)
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
                         unique_suffix="med_last_status")

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
                         unique_suffix="med_last_scheduled_at")

    @property
    def native_value(self) -> dt.datetime | None:
        rec = _latest_medication(self.coordinator)  # type: ignore[arg-type]
        return parse_hae_ts(rec.get("scheduledDate", "")) if rec else None
