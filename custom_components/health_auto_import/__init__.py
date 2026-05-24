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
    safe_slug,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Health Auto Import from a config entry."""
    host: str = entry.data[CONF_HOST]
    port: int = entry.data.get(CONF_PORT, DEFAULT_PORT)

    client = HaeClient(host, port)

    # 1. Reachability coordinator (always present — but skip first_refresh;
    #    discovery below will prove reachability and populate last_success).
    reachability = ReachabilityCoordinator(hass, client)

    # 2. Discovery — list tools + metric inventory.
    coordinators: dict[str, ToolCoordinator] = {}
    discovered_metrics: list[str] = []
    reachable = False

    try:
        discovery = await run_discovery(client)
        reachable = True
        _LOGGER.info(
            "Discovered %d tools and %d metrics on %s:%d",
            len(discovery.tools),
            len(discovery.metrics),
            host,
            port,
        )
        discovered_metrics = discovery.metrics

        # Fall back to previously persisted metrics if discovery found none
        # (e.g. server was down during the health_metrics enumeration call).
        if not discovered_metrics:
            saved_metrics = entry.options.get("discovered_metrics", [])
            if saved_metrics:
                _LOGGER.info(
                    "Metric discovery returned 0 metrics — using %d persisted metrics",
                    len(saved_metrics),
                )
                discovered_metrics = saved_metrics

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
            # Wire up new-metric detection for health_metrics.
            if tool_name == "health_metrics":
                coord.known_metrics = {
                    safe_slug(m) for m in discovered_metrics
                }
                coord.config_entry = entry
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

    # Seed reachability from discovery result to avoid an extra probe.
    reachability.data = reachable
    await reachability.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "client": client,
        "reachability": reachability,
        "coordinators": coordinators,
        "discovered_metrics": discovered_metrics,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry — persist watermarks before teardown."""
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if data:
        # Save watermarks and discovered metrics before unloading.
        coordinators = data.get("coordinators", {})
        wm_data = {
            name: coord.wm.to_dict() for name, coord in coordinators.items()
        }
        metrics = data.get("discovered_metrics", [])
        new_options = {
            **entry.options,
            "watermarks": wm_data,
            "discovered_metrics": metrics,
        }
        hass.config_entries.async_update_entry(entry, options=new_options)

    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload on options update."""
    await hass.config_entries.async_reload(entry.entry_id)
