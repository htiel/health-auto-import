"""Shared base entity for Health Auto Export."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

from .const import DOMAIN, MANUFACTURER, MODEL


class HaeEntity(CoordinatorEntity[DataUpdateCoordinator]):
    """Common entity — every HAE entity belongs to one device per config entry."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        *,
        entry_id: str,
        host: str,
        unique_suffix: str,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_{unique_suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            manufacturer=MANUFACTURER,
            model=MODEL,
            name=f"Health Auto Import ({host})",
            configuration_url=f"http://{host}",
        )
