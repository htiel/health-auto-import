"""Button entities for Health Auto Import."""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
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
from .coordinator import ReachabilityCoordinator, ToolCoordinator
from .entity import (
    DEVICE_HEALTH_METRICS,
    DEVICE_HEART,
    DEVICE_MEDICATIONS,
    DEVICE_SERVER,
    DEVICE_WORKOUTS,
    HaeEntity,
)

_LOGGER = logging.getLogger(__name__)

# Map tool names to (device_group, display_name, unique_suffix, icon).
_TOOL_BUTTON_META: dict[str, tuple[str, str, str, str]] = {
    TOOL_ECG: (DEVICE_HEART, "Sync ECG", "sync_ecg", "mdi:heart-pulse"),
    TOOL_HEART_NOTIFICATIONS: (DEVICE_HEART, "Sync heart notifications", "sync_hrn", "mdi:heart-flash"),
    TOOL_WORKOUTS: (DEVICE_WORKOUTS, "Sync workouts", "sync_workouts", "mdi:run"),
    TOOL_HEALTH_METRICS: (DEVICE_HEALTH_METRICS, "Sync health metrics", "sync_metrics", "mdi:chart-line"),
    TOOL_MEDICATIONS: (DEVICE_MEDICATIONS, "Sync medications", "sync_medications", "mdi:pill"),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    eid = entry.entry_id
    host = entry.data[CONF_HOST]

    buttons: list[ButtonEntity] = [
        SyncNowButton(
            reachability=data["reachability"],
            tool_coordinators=data["coordinators"],
            entry_id=eid,
            host=host,
        )
    ]

    # Per-tool sync buttons — one per discovered tool, on its own device.
    coordinators: dict[str, ToolCoordinator] = data.get("coordinators", {})
    for tool_name, coord in coordinators.items():
        meta = _TOOL_BUTTON_META.get(tool_name)
        if meta:
            device_group, name, suffix, icon = meta
            buttons.append(
                ToolSyncButton(
                    coord,
                    entry_id=eid,
                    host=host,
                    device_group=device_group,
                    name=name,
                    unique_suffix=suffix,
                    icon=icon,
                )
            )

    async_add_entities(buttons)


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


class ToolSyncButton(HaeEntity, ButtonEntity):
    """Trigger an immediate refresh of a single tool coordinator."""

    def __init__(
        self,
        coordinator: ToolCoordinator,
        *,
        entry_id: str,
        host: str,
        device_group: str,
        name: str,
        unique_suffix: str,
        icon: str,
    ) -> None:
        super().__init__(
            coordinator,
            entry_id=entry_id,
            host=host,
            unique_suffix=unique_suffix,
            device_group=device_group,
        )
        self._attr_name = name
        self._attr_icon = icon

    async def async_press(self) -> None:
        """Request an immediate data refresh from this tool's coordinator."""
        _LOGGER.info("Sync %s pressed", self._attr_name)
        await self.coordinator.async_request_refresh()
