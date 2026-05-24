"""Integration setup/teardown for Health Auto Import.

Security:
 - No secrets stored in ``hass.data`` (only client/coordinator references).
 - Discovery results sanitised via ``safe_slug`` before storage.
 - Options-update listener tears down cleanly (no orphan coordinators).
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .api import HaeClient
from .const import CONF_HOST, CONF_PORT, DEFAULT_PORT, DOMAIN, OPT_IN_TOOLS
from .coordinator import (
    ReachabilityCoordinator,
    ToolCoordinator,
    WatermarkState,
    run_discovery,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Health Auto Import from a config entry."""
    host: str = entry.data[CONF_HOST]
    port: int = entry.data.get(CONF_PORT, DEFAULT_PORT)

    client = HaeClient(host, port)

    # 1. Reachability coordinator (always present).
    reachability = ReachabilityCoordinator(hass, client)
    await reachability.async_config_entry_first_refresh()

    # 2. Discovery — probe tools + metric inventory.
    coordinators: dict[str, ToolCoordinator] = {}
    discovered_metrics: list[str] = []

    if reachability.data:  # Server is reachable on first load.
        try:
            discovery = await run_discovery(client)
            _LOGGER.info(
                "Discovered %d tools and %d metrics on %s:%d",
                len(discovery.tools),
                len(discovery.metrics),
                host,
                port,
            )
            discovered_metrics = discovery.metrics

            # Load persisted watermarks (if any) from config entry options.
            saved_wm: dict[str, dict] = entry.options.get("watermarks", {})

            # 3. Create one coordinator per discovered tool.
            for tool_name in discovery.tools:
                if tool_name in OPT_IN_TOOLS:
                    # Skip opt-in tools unless enabled by user.
                    if not entry.options.get(f"enable_{tool_name}", False):
                        continue
                wm = WatermarkState.from_dict(saved_wm.get(tool_name, {}))
                coord = ToolCoordinator(
                    hass,
                    client,
                    tool_name=tool_name,
                    watermark_state=wm,
                )
                coordinators[tool_name] = coord

            # First refresh for all coordinators.
            for coord in coordinators.values():
                try:
                    await coord.async_config_entry_first_refresh()
                except Exception:  # noqa: BLE001
                    _LOGGER.warning(
                        "Initial refresh failed for %s — will retry on next poll",
                        coord.tool_name,
                    )
        except Exception:  # noqa: BLE001
            _LOGGER.warning(
                "Discovery failed for %s:%d — integration will operate in "
                "reachability-only mode until next reload",
                host,
                port,
            )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "client": client,
        "reachability": reachability,
        "coordinators": coordinators,
        "discovered_metrics": discovered_metrics,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    # Persist watermarks on HA stop.
    async def _save_watermarks(_event: object) -> None:
        wm_data = {
            name: coord.wm.to_dict() for name, coord in coordinators.items()
        }
        new_options = {**entry.options, "watermarks": wm_data}
        hass.config_entries.async_update_entry(entry, options=new_options)

    entry.async_on_unload(
        hass.bus.async_listen_once("homeassistant_stop", _save_watermarks)
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry — persist watermarks before teardown."""
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if data:
        # Save watermarks before unloading.
        coordinators = data.get("coordinators", {})
        wm_data = {
            name: coord.wm.to_dict() for name, coord in coordinators.items()
        }
        new_options = {**entry.options, "watermarks": wm_data}
        hass.config_entries.async_update_entry(entry, options=new_options)

    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload on options update."""
    await hass.config_entries.async_reload(entry.entry_id)
