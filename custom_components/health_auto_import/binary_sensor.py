"""Binary sensors for Health Auto Export."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import ReachabilityCoordinator
from .entity import HaeEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    reachability: ReachabilityCoordinator = data["reachability"]
    async_add_entities(
        [
            ReachabilityBinarySensor(
                reachability,
                entry_id=entry.entry_id,
                host=entry.data[CONF_HOST],
            )
        ]
    )


class ReachabilityBinarySensor(HaeEntity, BinarySensorEntity):
    """ON when HAE server responded to the last TCP probe."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_name = "Reachable"

    def __init__(
        self,
        coordinator: ReachabilityCoordinator,
        *,
        entry_id: str,
        host: str,
    ) -> None:
        super().__init__(
            coordinator, entry_id=entry_id, host=host, unique_suffix="reachable"
        )

    @property
    def is_on(self) -> bool | None:
        return bool(self.coordinator.data) if self.coordinator.data is not None else None
