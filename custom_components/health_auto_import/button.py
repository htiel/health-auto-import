"""Button entities for Health Auto Import."""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import ReachabilityCoordinator, ToolCoordinator
from .entity import DEVICE_SERVER, HaeEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            SyncNowButton(
                reachability=data["reachability"],
                tool_coordinators=data["coordinators"],
                entry_id=entry.entry_id,
                host=entry.data[CONF_HOST],
            )
        ]
    )


class SyncNowButton(HaeEntity, ButtonEntity):
    """Trigger an immediate refresh of all tool coordinators."""

    _attr_name = "Sync now"
    _attr_icon = "mdi:sync"

    def __init__(
        self,
        reachability: ReachabilityCoordinator,
        tool_coordinators: dict[str, ToolCoordinator],
        *,
        entry_id: str,
        host: str,
    ) -> None:
        super().__init__(
            reachability,
            entry_id=entry_id,
            host=host,
            unique_suffix="sync_now",
            device_group=DEVICE_SERVER,
        )
        self._tool_coordinators = tool_coordinators

    async def async_press(self) -> None:
        """Request an immediate data refresh from all coordinators."""
        _LOGGER.info("Sync Now pressed — refreshing all coordinators")
        # Refresh reachability first.
        await self.coordinator.async_request_refresh()
        # Then refresh every tool coordinator.
        for coord in self._tool_coordinators.values():
            await coord.async_request_refresh()
