"""Shared base entity for Health Auto Export."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

from .const import DOMAIN, MANUFACTURER


# Device group constants — each maps to a separate device in the HA UI.
DEVICE_SERVER = "server"
DEVICE_HEART = "heart"
DEVICE_WORKOUTS = "workouts"
DEVICE_MEDICATIONS = "medications"
DEVICE_HEALTH_METRICS = "health_metrics"

_DEVICE_META: dict[str, tuple[str, str]] = {
    # group -> (display name suffix, model)
    DEVICE_SERVER: ("Server", "HAE TCP/MCP Server"),
    DEVICE_HEART: ("Heart", "Apple Health — Heart"),
    DEVICE_WORKOUTS: ("Workouts", "Apple Health — Workouts"),
    DEVICE_MEDICATIONS: ("Medications", "Apple Health — Medications"),
    DEVICE_HEALTH_METRICS: ("Health Metrics", "Apple Health — Metrics"),
}


class HaeEntity(CoordinatorEntity[DataUpdateCoordinator]):
    """Common entity — each device_group gets its own device in the HA UI."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        *,
        entry_id: str,
        host: str,
        unique_suffix: str,
        device_group: str = DEVICE_SERVER,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_{unique_suffix}"

        suffix, model = _DEVICE_META.get(device_group, _DEVICE_META[DEVICE_SERVER])
        info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_{device_group}")},
            manufacturer=MANUFACTURER,
            model=model,
            name=f"Health Auto Import — {suffix}",
        )
        # Child devices link back to the server device.
        if device_group != DEVICE_SERVER:
            info["via_device"] = (DOMAIN, f"{entry_id}_{DEVICE_SERVER}")
        self._attr_device_info = info
